"""Authority, lifecycle and identity-binding regressions for both transports."""

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from core.db import get_session
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from auth.api import (
    Authority,
    Principal,
    PrincipalKind,
    anonymous_principal,
    get_principal,
)
from auth.dependencies import reset_current_principal, set_current_principal
from auth.platform import mcp, service
from auth.platform.enrollment import (
    AUDIENCE,
    TOKEN_TYPE,
    Completion,
    ReceiptVerifier,
    acknowledge_activation,
    activate,
)
from auth.platform.models import (
    PlatformAudit,
    PlatformCommand,
    PlatformIdentity,
    PlatformInvitation,
    PlatformUser,
    now,
)
from auth.platform.router import router

OPERATOR = Principal(
    subject="operator",
    issuer="https://idp.test/grimoire/",
    email="owner@example.test",
    kind=PrincipalKind.HUMAN,
    authority=Authority.STANDING,
    actor=(),
    scope=(),
    groups=("operators",),
    user_type="internal",
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_AUTHENTIK_ISSUER", OPERATOR.issuer)
    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ENABLED", "true")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'accounts.db'}",
        execution_options={"schema_translate_map": {"platform_auth": None}},
    )
    tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.schema == "platform_auth"
    ]
    SQLModel.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        service.command(
            session,
            OPERATOR,
            "bootstrap",
            request_id="bootstrap-owner",
            reason="Explicit operator import",
        )
    yield engine
    engine.dispose()


def command(session, action, **arguments):
    return service.command(
        session,
        OPERATOR,
        action,
        request_id=str(uuid4()),
        reason="Test authorized operation",
        **arguments,
    )


def invite(session, email="player@example.test"):
    row = command(session, "issue", recipient_label=email, expires_in_days=1)
    delivered = command(session, "deliver", invitation_id=row["id"], reissue=False)
    token = delivered.pop("token")
    proof = Completion(
        OPERATOR.issuer,
        "player",
        "player",
        row["id"],
        hashlib.sha256(token.encode()).hexdigest(),
        str(uuid4()),
    )
    return row, token, proof


@pytest.mark.parametrize(
    "principal",
    [
        anonymous_principal(),
        replace(OPERATOR, groups=()),
        replace(OPERATOR, kind=PrincipalKind.WORKLOAD),
        replace(OPERATOR, authority=Authority.DELEGATED),
        replace(OPERATOR, actor=("other",)),
        replace(OPERATOR, subject="unbootstrapped"),
        replace(OPERATOR, issuer=""),
    ],
)
def test_management_rejects_unqualified_actors(database, principal):
    with Session(database) as session:
        with pytest.raises(HTTPException) as error:
            service.command(
                session,
                principal,
                "issue",
                request_id="denied-issue",
                reason="Denied",
                recipient_label="Friend",
                expires_in_days=1,
            )
        assert error.value.status_code == 403
        assert len(session.exec(select(PlatformUser)).all()) == 1
        assert session.exec(select(PlatformInvitation)).all() == []


def test_preparation_replay_never_mints_or_extends(database):
    with Session(database) as session:
        args = {
            "request_id": "prepare-0001",
            "reason": "Invite a friend",
            "recipient_label": "Friend",
            "expires_in_days": 1,
        }
        result = service.command(session, OPERATOR, "issue", **args)
        assert result == service.command(session, OPERATOR, "issue", **args)
        row = session.get(PlatformInvitation, result["id"])
        assert row.token_digest is None
        assert len(session.exec(select(PlatformInvitation)).all()) == 1
        with pytest.raises(HTTPException) as error:
            service.command(
                session,
                OPERATOR,
                "issue",
                **{**args, "recipient_label": "Other friend"},
            )
        assert error.value.status_code == 409


def test_delivery_rotation_invalidates_old_capability_and_never_persists_raw(database):
    with Session(database) as session:
        row, token, proof = invite(session)
        expires = session.get(PlatformInvitation, row["id"]).expires_at
        assert service.inspect_invitation(session, token).id == row["id"]
        rotated = command(session, "deliver", invitation_id=row["id"], reissue=True)
        with pytest.raises(HTTPException):
            service.inspect_invitation(session, token)
        assert service.inspect_invitation(session, rotated["token"]).id == row["id"]
        assert session.get(PlatformInvitation, row["id"]).expires_at == expires
        for entry in session.exec(select(PlatformCommand)).all():
            assert (
                token not in entry.result_json
                and rotated["token"] not in entry.result_json
            )
        with pytest.raises(HTTPException):
            activate(session, proof)


def test_completion_is_identity_bound_replay_safe_and_grants_nothing(database):
    with Session(database) as session:
        row, _, proof = invite(session)
        result = activate(session, proof)
        assert result["permissions"] == []
        assert activate(session, proof) == result
        assert len(session.exec(select(PlatformIdentity)).all()) == 2
        assert (
            len(
                session.exec(
                    select(PlatformAudit).where(PlatformAudit.action == "activate")
                ).all()
            )
            == 1
        )
        with pytest.raises(HTTPException):
            activate(session, replace(proof, subject="another-player"))
        assert (
            session.get(PlatformInvitation, row["id"]).accepted_user_id == result["id"]
        )


def test_activation_acknowledgement_is_bound_idempotent_and_cannot_be_completion(
    database,
):
    with Session(database) as session:
        _, token, proof = invite(session)
        assert activate(session, proof)["activation_pending"] is True
        with pytest.raises(HTTPException):
            acknowledge_activation(session, proof)
        activated = replace(proof, phase="activated")
        with pytest.raises(HTTPException):
            activate(session, activated)
        assert acknowledge_activation(session, activated) == {
            "activation_pending": False
        }
        assert acknowledge_activation(session, activated) == {
            "activation_pending": False
        }
        assert activate(session, proof)["activation_pending"] is False
        response = client(database, anonymous_principal()).post(
            "/api/auth/platform/enrollment/validate", json={"token": token}
        )
        assert (
            response.status_code == 200
            and response.json()["activation_pending"] is False
        )
        with pytest.raises(HTTPException):
            acknowledge_activation(
                session, replace(activated, subject="different-user")
            )


@pytest.mark.parametrize(
    "principal",
    [
        replace(OPERATOR, user_type=None),
        replace(OPERATOR, delegation_claim_present=True),
        replace(OPERATOR, issuer="https://agent.test/"),
    ],
)
def test_management_denies_legacy_classification_delegation_and_agent_issuer(
    database, principal
):
    with Session(database) as session:
        with pytest.raises(HTTPException) as error:
            service.command(
                session,
                principal,
                "bootstrap",
                request_id="denied-bootstrap",
                reason="Denied source",
            )
        assert error.value.status_code == 403


def test_bootstrap_links_only_the_authenticated_operator_with_explicit_target(
    database, monkeypatch
):
    second = replace(
        OPERATOR, issuer="https://second-idp.test/", subject=OPERATOR.subject
    )
    monkeypatch.setenv("PLATFORM_AUTH_LOGIN_ISSUER", second.issuer)
    with Session(database) as session:
        owner = service.identity_user(session, OPERATOR)
        result = service.command(
            session,
            second,
            "bootstrap",
            request_id="explicit-link",
            reason="Link my second login",
            user_id=owner.id,
        )
        assert result["id"] == owner.id and result["permissions"] == []
        assert len(result["identities"]) == 2
        assert len(session.exec(select(PlatformUser)).all()) == 1


@pytest.mark.parametrize("case", ["revoked", "expired", "invitation", "digest"])
def test_bad_binding_and_invalid_invitation_do_not_activate(database, case):
    with Session(database) as session:
        row, _, proof = invite(session)
        if case == "revoked":
            command(session, "revoke_invitation", invitation_id=row["id"])
        elif case == "expired":
            stored = session.get(PlatformInvitation, row["id"])
            stored.expires_at = now() - timedelta(seconds=1)
            session.commit()
        elif case == "invitation":
            proof = replace(proof, invitation_id=str(uuid4()))
        else:
            proof = replace(proof, invitation_digest="0" * 64)
        with pytest.raises(HTTPException):
            activate(session, proof)
        assert len(session.exec(select(PlatformUser)).all()) == 1


def test_matching_username_never_merges_other_identity(database):
    with Session(database) as session:
        _, _, first = invite(session)
        activate(session, first)
        _, _, second = invite(session)
        with pytest.raises(HTTPException) as error:
            activate(session, replace(second, subject="different-identity"))
        assert error.value.status_code == 409
        assert len(session.exec(select(PlatformUser)).all()) == 2


def test_live_grants_and_disabled_users_are_rechecked(database):
    with Session(database) as session:
        _, _, proof = invite(session)
        enrolled = activate(session, proof)
        player = replace(
            OPERATOR,
            subject=proof.subject,
            email=proof.email,
            username=proof.username,
            groups=(),
        )
        with pytest.raises(HTTPException):
            service.require_permission(session, player, "grimoire.access")
        command(session, "grant", user_id=enrolled["id"], permission="grimoire.access")
        assert (
            service.require_permission(session, player, "grimoire.access").id
            == enrolled["id"]
        )
        with pytest.raises(HTTPException):
            service.require_permission(session, player, "grimoire.create_game")
        for permission in (
            "operators",
            "moving.access",
            "authentik.add_invitation",
            "grimoire.admin",
        ):
            with pytest.raises(HTTPException):
                command(session, "grant", user_id=enrolled["id"], permission=permission)
        command(
            session,
            "revoke_grant",
            user_id=enrolled["id"],
            permission="grimoire.access",
        )
        with pytest.raises(HTTPException):
            service.require_permission(session, player, "grimoire.access")
        command(session, "grant", user_id=enrolled["id"], permission="grimoire.access")
        command(session, "set_active", user_id=enrolled["id"], active=False)
        with pytest.raises(HTTPException):
            service.require_permission(session, player, "grimoire.access")
        with pytest.raises(HTTPException):
            activate(session, proof)


def test_disabled_operator_cannot_administer_or_replay(database):
    with Session(database) as session:
        owner = service.identity_user(session, OPERATOR)
        command(session, "set_active", user_id=owner.id, active=False)
        with pytest.raises(HTTPException):
            command(session, "bootstrap")
        with pytest.raises(HTTPException):
            service.read(session, OPERATOR, "users")


def client(database, principal):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: principal

    def session():
        with Session(database) as connection:
            yield connection

    app.dependency_overrides[get_session] = session
    return TestClient(app)


@pytest.mark.parametrize(
    "changes",
    [
        {"authority": Authority.DELEGATED},
        {"kind": PrincipalKind.WORKLOAD},
        {"user_type": "service_account"},
        {"delegation_claim_present": True},
        {"actor": ("delegator",)},
    ],
)
def test_own_profile_requires_standing_human(database, changes):
    response = client(database, replace(OPERATOR, **changes)).get(
        "/api/auth/platform/self"
    )
    assert response.status_code == 403


def test_http_mcp_authorization_parity_and_mcp_no_capabilities(database, monkeypatch):
    monkeypatch.setattr(mcp, "get_engine", lambda: database)
    player = replace(OPERATOR, groups=())
    body = {
        "action": "issue",
        "request_id": "issue-parity",
        "reason": "Invite friend",
        "arguments": {"recipient_label": "Friend", "expires_in_days": 1},
    }
    response = client(database, player).post("/api/auth/platform/commands", json=body)
    assert response.status_code == 403
    context = set_current_principal(player)
    try:
        denied = asyncio.run(
            mcp.platform_invitation_issue(
                "player@example.test", "issue-parity", "Invite friend", 1
            )
        )
    finally:
        reset_current_principal(context)
    assert denied["status"] == response.status_code
    context = set_current_principal(OPERATOR)
    try:
        prepared = asyncio.run(
            mcp.platform_invitation_issue(
                "player@example.test", "issue-parity", "Invite friend", 1
            )
        )
    finally:
        reset_current_principal(context)
    assert "token" not in prepared and prepared["status"] == "awaiting_delivery"
    response = client(database, OPERATOR).post(
        f"/api/auth/platform/invitations/{prepared['id']}/deliver",
        json={"request_id": "deliver-parity", "reason": "Deliver to friend"},
    )
    assert response.status_code == 200 and len(response.json()["token"]) == 43
    assert response.headers["cache-control"] == "no-store"


def test_invalid_enrollment_body_does_not_echo_capability(database):
    response = client(database, anonymous_principal()).post(
        "/api/auth/platform/enrollment/validate",
        json={"token": "sensitive-value", "password": "another-sensitive-value"},
    )
    assert response.status_code == 422
    assert "sensitive-value" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_disabled_features_are_unavailable_without_database(database, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "false")
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ENABLED", "false")
    app = client(database, OPERATOR)
    assert app.get("/api/auth/platform/management/users").status_code == 404
    assert (
        app.post(
            "/api/auth/platform/enrollment/validate", json={"token": "a" * 43}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "change",
    [
        "login",
        "audience",
        "issuer",
        "binding",
        "expired",
        "long_lifetime",
        "digest",
        "signature",
        "subject",
    ],
)
def test_completion_verifier_rejects_untrusted_or_ordinary_tokens(database, change):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"

    class Keys:
        async def get_key(self, kid, **kwargs):
            return jwk if kid == "test-key" else None

    instant = int(time.time())
    claims = {
        "iss": OPERATOR.issuer,
        "sub": "player",
        "aud": AUDIENCE,
        "iat": instant,
        "nbf": instant,
        "exp": instant + 60,
        "jti": str(uuid4()),
        "username": "player",
        "invitation_bound": True,
        "phase": "completed",
        "invitation_id": str(uuid4()),
        "invitation_digest": "a" * 64,
    }
    verifier = ReceiptVerifier(OPERATOR.issuer, Keys())
    valid = jwt.encode(
        claims, key, algorithm="RS256", headers={"kid": "test-key", "typ": TOKEN_TYPE}
    )
    assert asyncio.run(verifier.verify(valid)).subject == "player"
    headers = {"kid": "test-key", "typ": TOKEN_TYPE}
    signing = key
    if change == "login":
        headers["typ"] = "JWT"
    elif change == "audience":
        claims["aud"] = "grimoire"
    elif change == "issuer":
        claims["iss"] = "https://other-idp.test/"
    elif change == "binding":
        claims["invitation_bound"] = False
    elif change == "expired":
        claims.update(iat=instant - 120, nbf=instant - 120, exp=instant - 60)
    elif change == "long_lifetime":
        claims["exp"] = instant + 1000
    elif change == "digest":
        claims["invitation_digest"] = "not-a-digest"
    elif change == "subject":
        claims["sub"] = ""
    else:
        signing = other
    invalid = jwt.encode(claims, signing, algorithm="RS256", headers=headers)
    with pytest.raises(HTTPException) as error:
        asyncio.run(verifier.verify(invalid))
    assert error.value.status_code == 401
