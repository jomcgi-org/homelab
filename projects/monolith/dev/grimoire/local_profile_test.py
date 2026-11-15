"""The local profile fixture cannot register account mutations or enrollment."""

from dataclasses import replace

import pytest
from auth.api import Authority, Principal, PrincipalKind
from auth.platform import enrollment, service
from auth.platform.identity import browser_or_operator
from auth.platform.models import PlatformGrant, PlatformIdentity, PlatformUser
from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlmodel import Session, create_engine, select

from dev.grimoire.app import register_local_profile, seed_local_profiles

ISSUER = "http://127.0.0.1:8177/__local/"
PLAYER = Principal(
    subject="a",
    issuer=ISSUER,
    email="a@example.test",
    kind=PrincipalKind.HUMAN,
    authority=Authority.STANDING,
    actor=(),
    scope=(),
    groups=(),
    user_type="external",
)


@pytest.fixture
def database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'local-profiles.db'}",
        execution_options={"schema_translate_map": {"platform_auth": None}},
    )
    seed_local_profiles(engine, ISSUER)
    yield engine
    engine.dispose()


def test_only_existing_demo_identities_are_seeded_once_without_grants(database):
    with Session(database) as session:
        before = {user.username: user.id for user in session.exec(select(PlatformUser))}
    seed_local_profiles(database, ISSUER)
    assert set(inspect(database).get_table_names()) == {"user", "identity", "grant"}
    with Session(database) as session:
        users = session.exec(select(PlatformUser)).all()
        assert {user.username: user.id for user in users} == before
        assert {user.username: user.display_name for user in users} == {
            "dm": "Rowan",
            "a": "Elowen",
            "b": "Bram",
        }
        assert all(
            user.active and user.email == f"{user.username}@example.test"
            for user in users
        )
        assert {
            (row.issuer, row.subject, row.user_id)
            for row in session.exec(select(PlatformIdentity))
        } == {(ISSUER, username, user_id) for username, user_id in before.items()}
        assert session.exec(select(PlatformGrant)).all() == []


@pytest.mark.parametrize(
    "subject, expected", [("dm", 200), ("a", 200), ("b", 200), ("unknown", 403)]
)
def test_profile_is_read_only_and_cannot_reach_management_or_enrollment(
    database, monkeypatch, subject, expected
):
    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ENABLED", "false")

    def forbidden(*args, **kwargs):
        pytest.fail(
            "The local profile must never invoke account administration or enrollment"
        )

    monkeypatch.setattr(service, "command", forbidden)
    monkeypatch.setattr(enrollment, "activate", forbidden)
    monkeypatch.setattr(enrollment, "receipt_verifier", forbidden)
    app = FastAPI()
    register_local_profile(app)
    assert {
        path: set(operations) for path, operations in app.openapi()["paths"].items()
    } == {"/api/auth/platform/self": {"get"}}

    def session():
        with Session(database) as db:
            yield db

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[browser_or_operator] = lambda: replace(
        PLAYER, subject=subject
    )
    statements = []
    event.listen(
        database,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(
            statement
        ),
    )
    with TestClient(app) as client:
        response = client.get("/api/auth/platform/self")
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        if expected == 200:
            assert response.json()["username"] == subject
            assert response.json()["permissions"] == []
        for path in (
            "/commands",
            "/invitations/local/deliver",
            "/enrollment/validate",
            "/enrollment/complete",
        ):
            assert client.post("/api/auth/platform" + path, json={}).status_code == 404
        assert client.get("/api/auth/platform/management/users").status_code == 404
        assert client.post("/api/auth/platform/self", json={}).status_code == 405
    assert statements and all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
