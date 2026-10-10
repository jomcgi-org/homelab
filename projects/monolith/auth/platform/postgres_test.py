"""Observe real PostgreSQL serialization and private migration effects."""

import hashlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from auth.api import Authority, Principal, PrincipalKind
from auth.platform.enrollment import Completion, activate
from auth.platform.models import (
    PlatformAudit,
    PlatformIdentity,
    PlatformInvitation,
    now,
)
from auth.platform.service import command


@pytest.fixture
def lane(pg, monkeypatch):
    monkeypatch.setenv("AUTH_AUTHENTIK_ISSUER", "https://idp.test/")
    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ENABLED", "true")
    name = "platform-test-" + uuid4().hex
    engines = [
        create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": name + suffix,
                "options": "-c lock_timeout=15000 -c statement_timeout=20000",
            },
        )
        for suffix in ("-first", "-second", "-observer")
    ]
    yield engines, name
    for engine in engines:
        engine.dispose()


def seed(engine):
    suffix = uuid4().hex
    token = secrets.token_urlsafe(32)
    operator = Principal(
        issuer="https://idp.test/",
        subject="operator-" + suffix,
        email=f"{suffix}@example.test",
        actor=(),
        scope=(),
        groups=("operators",),
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
        user_type="internal",
    )
    with Session(engine) as session:
        command(
            session,
            operator,
            "bootstrap",
            request_id=uuid4().hex,
            reason="Explicit operator bootstrap",
        )
        invitation = PlatformInvitation(
            recipient_label="Friend",
            issued_by="test",
            status="pending",
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=now() + timedelta(hours=1),
        )
        session.add(invitation)
        session.commit()
        proof = Completion(
            operator.issuer,
            "player-" + suffix,
            "player-" + suffix[:16],
            invitation.id,
            invitation.token_digest,
            str(uuid4()),
        )
        return operator, proof


def race(lane, first, second):
    engines, name = lane
    locked, release = Event(), Event()

    def run(index, operation):
        with Session(engines[index]) as session:
            original = session.exec

            def pause(statement, *args, **kwargs):
                result = original(statement, *args, **kwargs)
                sql = str(statement.compile(dialect=postgresql.dialect()))
                if (
                    index == 0
                    and not locked.is_set()
                    and "FROM platform_auth.invitation" in sql
                    and "FOR UPDATE" in sql
                ):
                    locked.set()
                    assert release.wait(10), "Invitation lock was not released"
                return result

            session.exec = pause
            try:
                return operation(session)
            except HTTPException as error:
                session.rollback()
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run, 0, first)
        try:
            assert locked.wait(10), "First transaction never held the invitation lock"
            two = pool.submit(run, 1, second)
            deadline = time.monotonic() + 10
            with (
                engines[2]
                .connect()
                .execution_options(isolation_level="AUTOCOMMIT") as observer
            ):
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = :name AND wait_event_type = 'Lock')"
                        ),
                        {"name": name + "-second"},
                    ).scalar()
                    if waiting:
                        break
                    if two.done():
                        pytest.fail(
                            "Second transaction finished without the expected lock wait"
                        )
                    time.sleep(0.02)
                else:
                    pytest.fail(
                        "PostgreSQL never reported the second transaction waiting on a lock"
                    )
        finally:
            release.set()
        return one.result(timeout=10), two.result(timeout=10)


@pytest.mark.parametrize("same_identity", [True, False])
def test_concurrent_completion_consumes_one_identity_and_one_audit(lane, same_identity):
    _, proof = seed(lane[0][2])
    other = (
        replace(proof, request_id=str(uuid4()))
        if same_identity
        else replace(
            proof,
            subject="stranger-" + uuid4().hex,
            username="stranger-" + uuid4().hex[:16],
        )
    )
    first, second = race(
        lane,
        lambda session: activate(session, proof),
        lambda session: activate(session, other),
    )
    assert not isinstance(first, HTTPException)
    if same_identity:
        assert second == first
    else:
        assert isinstance(second, HTTPException) and second.status_code == 403
    with Session(lane[0][2]) as session:
        row = session.get(PlatformInvitation, proof.invitation_id)
        assert row.status == "accepted" and row.accepted_subject == proof.subject
        assert (
            len(
                session.exec(
                    select(PlatformIdentity).where(
                        PlatformIdentity.user_id == row.accepted_user_id
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(PlatformAudit).where(
                        PlatformAudit.target == row.accepted_user_id,
                        PlatformAudit.action == "activate",
                    )
                ).all()
            )
            == 1
        )


def test_revoke_wins_while_completion_is_waiting(lane):
    operator, proof = seed(lane[0][2])
    first, second = race(
        lane,
        lambda session: command(
            session,
            operator,
            "revoke_invitation",
            request_id=uuid4().hex,
            reason="Revoke before completion",
            invitation_id=proof.invitation_id,
        ),
        lambda session: activate(session, proof),
    )
    assert first["status"] == "revoked"
    assert isinstance(second, HTTPException) and second.status_code in (403, 410)
    with Session(lane[0][2]) as session:
        assert (
            session.exec(
                select(PlatformIdentity).where(
                    PlatformIdentity.subject == proof.subject
                )
            ).first()
            is None
        )


def test_migration_keeps_public_database_roles_out(lane):
    with lane[0][2].connect() as connection:
        for role in ("public_reader", "public_writer"):
            assert (
                connection.execute(
                    text(
                        "SELECT has_schema_privilege(:role, 'platform_auth', 'USAGE')"
                    ),
                    {"role": role},
                ).scalar()
                is False
            )
            for table in (
                "user",
                "identity",
                "invitation",
                "grant",
                "command",
                "audit",
            ):
                assert (
                    connection.execute(
                        text(
                            "SELECT has_table_privilege(:role, :table, 'SELECT,INSERT,UPDATE,DELETE')"
                        ),
                        {"role": role, "table": f'platform_auth."{table}"'},
                    ).scalar()
                    is False
                )
