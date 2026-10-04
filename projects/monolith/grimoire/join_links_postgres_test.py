"""Single-use campaign join links against real PostgreSQL transactions.

The concurrency oracle observes a real database lock wait before releasing the
first transaction. A thread merely starting is not evidence of serialization.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire import join_links
from grimoire.join_links import issue_link, redeem_link, revoke_link
from grimoire.models import AppUser, Campaign, CampaignJoinLink, CampaignMember
from grimoire.router import revoke_player

WAIT_SECONDS = 10
ISSUER = "https://auth.example/application/o/grimoire/"
_CAMPAIGN_FROM = re.compile(r"FROM\s+grimoire\.campaign(?!\w)")


@pytest.fixture
def lane(pg):
    identity = uuid4().hex
    engines = []

    def engine(actor):
        value = create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": f"join-link-{identity}-{actor}",
                "connect_timeout": WAIT_SECONDS,
                "options": "-c lock_timeout=15000 -c statement_timeout=20000",
            },
        )
        engines.append(value)
        return value

    observer = engine("observer")
    try:
        yield SimpleNamespace(identity=identity, engine=engine, observer=observer)
    finally:
        for value in engines:
            value.dispose()


def _seed(lane):
    with Session(lane.observer) as session:
        owner = AppUser(
            email=f"owner-{lane.identity}@example.test",
            issuer=ISSUER,
            subject=f"owner-{lane.identity}",
        )
        player = AppUser(
            email=f"player-{lane.identity}@example.test",
            issuer=ISSUER,
            subject=f"player-{lane.identity}",
        )
        stranger = AppUser(
            email=f"stranger-{lane.identity}@example.test",
            issuer=ISSUER,
            subject=f"stranger-{lane.identity}",
        )
        session.add_all([owner, player, stranger])
        session.flush()
        campaign = Campaign(
            name=f"Join link concurrency {lane.identity}",
            owner_app_user_id=owner.id,
        )
        session.add(campaign)
        session.flush()
        session.add(
            CampaignMember(campaign_id=campaign.id, app_user_id=owner.id, role="dm")
        )
        token = secrets.token_urlsafe(32)
        link = CampaignJoinLink(
            campaign_id=campaign.id,
            recipient_id=player.id,
            invitee_email=player.email,
            issued_by_id=owner.id,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            enrollment_allowed=False,
            enrollment_username=f"grimoire-{uuid4().hex}",
        )
        session.add(link)
        session.commit()
        return SimpleNamespace(
            campaign_id=campaign.id,
            owner_id=owner.id,
            owner_email=owner.email,
            player_id=player.id,
            player_email=player.email,
            stranger_id=stranger.id,
            link_id=link.id,
            token=token,
        )


def _redeem(session, seed, user_id=None):
    user = session.get(AppUser, user_id or seed.player_id)
    return redeem_link(session, seed.token, user)


def _revoke(session, seed):
    return revoke_link(
        session, seed.campaign_id, seed.link_id, session.get(AppUser, seed.owner_id)
    )


def _state(lane, seed):
    with Session(lane.observer) as session:
        row = session.get(CampaignJoinLink, seed.link_id)
        memberships = session.exec(
            select(CampaignMember).where(
                CampaignMember.campaign_id == seed.campaign_id,
                CampaignMember.app_user_id == seed.player_id,
            )
        ).all()
        return SimpleNamespace(
            status=row.status,
            recipient_id=row.recipient_id,
            accepted_by_id=row.accepted_by_id,
            member_ids=[member.id for member in memberships],
            member_roles=[member.role for member in memberships],
            character_ids=[member.player_character_id for member in memberships],
        )


def _is_campaign_lock(statement):
    sql = str(statement.compile(dialect=postgresql.dialect()))
    return bool(_CAMPAIGN_FROM.search(sql)) and (
        "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql
    )


def _ordered_race(lane, first, second):
    """Hold the first campaign lock until PostgreSQL blocks the second."""
    locked = Event()
    release = Event()

    def run(actor, action):
        with Session(lane.engine(actor)) as session:
            original_exec = session.exec

            def paused_exec(statement, *args, **kwargs):
                result = original_exec(statement, *args, **kwargs)
                if (
                    actor == "first"
                    and not locked.is_set()
                    and _is_campaign_lock(statement)
                ):
                    locked.set()
                    assert release.wait(WAIT_SECONDS), "campaign lock never released"
                return result

            session.exec = paused_exec
            try:
                return action(session)
            except HTTPException as exc:
                session.rollback()
                return exc
            finally:
                session.exec = original_exec

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run, "first", first)
        try:
            if not locked.wait(WAIT_SECONDS):
                if first_future.done():
                    # Surface the original database/application error instead
                    # of hiding it behind a concurrency-oracle timeout.
                    outcome = first_future.result()
                    pytest.fail(
                        f"first operation returned before its lock: {outcome!r}"
                    )
                pytest.fail("first operation did not lock campaign")
            second_future = pool.submit(run, "second", second)
            application_name = f"join-link-{lane.identity}-second"
            deadline = time.monotonic() + WAIT_SECONDS
            with lane.observer.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as observer:
                while time.monotonic() < deadline:
                    waiting = observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = :name "
                            "AND wait_event_type = 'Lock' "
                            "AND cardinality(pg_blocking_pids(pid)) > 0)"
                        ),
                        {"name": application_name},
                    )
                    if waiting:
                        break
                    if second_future.done():
                        outcome = second_future.result()
                        pytest.fail(
                            "second operation returned without waiting for the "
                            f"campaign lock: {outcome!r}"
                        )
                    time.sleep(0.01)
                else:
                    pytest.fail(
                        "PostgreSQL never observed the second operation blocked"
                    )
        finally:
            release.set()
        return (
            first_future.result(timeout=WAIT_SECONDS + 20),
            second_future.result(timeout=WAIT_SECONDS + 20),
        )


def test_concurrent_same_recipient_redemptions_create_exactly_one_membership(lane):
    seed = _seed(lane)
    first, second = _ordered_race(
        lane,
        lambda session: _redeem(session, seed),
        lambda session: _redeem(session, seed),
    )
    assert not isinstance(first, HTTPException)
    assert not isinstance(second, HTTPException)
    state = _state(lane, seed)
    assert state.status == "accepted"
    assert state.accepted_by_id == seed.player_id
    assert len(state.member_ids) == 1
    assert state.member_roles == ["player"]
    assert state.character_ids == [None]


def test_concurrent_issuance_does_not_mint_multiple_pending_links(lane):
    seed = _seed(lane)

    def issue(session):
        owner = session.get(AppUser, seed.owner_id)
        stranger = session.get(AppUser, seed.stranger_id)
        return issue_link(session, seed.campaign_id, owner, stranger.email)

    first, second = _ordered_race(lane, issue, issue)
    assert not isinstance(first, HTTPException)
    assert isinstance(second, HTTPException)
    assert second.status_code == 409
    with Session(lane.observer) as session:
        rows = session.exec(
            select(CampaignJoinLink).where(
                CampaignJoinLink.campaign_id == seed.campaign_id,
                CampaignJoinLink.recipient_id == seed.stranger_id,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].token_digest == hashlib.sha256(first.token.encode()).hexdigest()
        assert first.token not in repr(rows[0].model_dump())


def test_enrollment_identity_is_stable_across_campaigns(lane, monkeypatch):
    seed = _seed(lane)
    monkeypatch.setattr(join_links, "enrollment_enabled", lambda: True)
    # Issuance checks configuration only. No Authentik network operation is
    # allowed in this database test, even if the host has credentials set.
    monkeypatch.setattr(join_links, "InvitationProvider", lambda: object())
    invitee_email = f"new-{lane.identity}@example.test"
    with Session(lane.observer) as session:
        owner = session.get(AppUser, seed.owner_id)
        other_campaign = Campaign(
            name=f"Other campaign {lane.identity}", owner_app_user_id=owner.id
        )
        session.add(other_campaign)
        session.commit()
        first = issue_link(
            session,
            seed.campaign_id,
            owner,
            invitee_email.upper(),
            allow_enrollment=True,
            can_administer_accounts=True,
        )
        second = issue_link(
            session,
            other_campaign.id,
            owner,
            invitee_email,
            allow_enrollment=True,
            can_administer_accounts=True,
        )
        first_row = session.get(CampaignJoinLink, first.id)
        second_row = session.get(CampaignJoinLink, second.id)
        assert first_row.recipient_id is None and second_row.recipient_id is None
        assert first_row.enrollment_allowed and second_row.enrollment_allowed
        assert first_row.enrollment_username == second_row.enrollment_username
        assert first_row.enrollment_username
        assert first.token != second.token


def test_revocation_wins_before_waiting_redemption(lane):
    seed = _seed(lane)
    first, second = _ordered_race(
        lane,
        lambda session: _revoke(session, seed),
        lambda session: _redeem(session, seed),
    )
    assert first is None
    assert isinstance(second, HTTPException)
    assert second.status_code in (409, 410)
    state = _state(lane, seed)
    assert state.status == "revoked"
    assert state.accepted_by_id is None
    assert state.member_ids == []


def test_redemption_wins_before_waiting_revocation(lane):
    seed = _seed(lane)
    first, second = _ordered_race(
        lane,
        lambda session: _redeem(session, seed),
        lambda session: _revoke(session, seed),
    )
    assert not isinstance(first, HTTPException)
    assert isinstance(second, HTTPException)
    assert second.status_code == 409
    state = _state(lane, seed)
    assert state.status == "accepted"
    assert state.accepted_by_id == seed.player_id
    assert len(state.member_ids) == 1


def test_membership_removal_wins_before_waiting_accepted_link_replay(lane):
    seed = _seed(lane)
    with Session(lane.observer) as session:
        _redeem(session, seed)
    member_id = _state(lane, seed).member_ids[0]

    def remove(session):
        session.info["grimoire_user_id"] = seed.owner_id
        return revoke_player(seed.campaign_id, member_id, seed.owner_email, session)

    first, second = _ordered_race(lane, remove, lambda session: _redeem(session, seed))
    assert first is None
    assert isinstance(second, HTTPException)
    assert second.status_code in (409, 410)
    state = _state(lane, seed)
    assert state.status == "revoked"
    assert state.member_ids == []


def test_registered_recipient_binding_survives_email_change_and_reuse(lane):
    seed = _seed(lane)
    with Session(lane.observer) as session:
        player = session.get(AppUser, seed.player_id)
        player.email = f"changed-{lane.identity}@example.test"
        session.commit()
        stranger = session.get(AppUser, seed.stranger_id)
        stranger.email = seed.player_email
        session.commit()
        with pytest.raises(HTTPException) as error:
            _redeem(session, seed, seed.stranger_id)
        assert error.value.status_code in (403, 404)
        session.rollback()
        assert _state(lane, seed).status == "pending"
        _redeem(session, seed)
    state = _state(lane, seed)
    assert state.accepted_by_id == seed.player_id
    assert len(state.member_ids) == 1


def test_expired_pending_link_does_not_create_membership(lane):
    seed = _seed(lane)
    with Session(lane.observer) as session:
        link = session.get(CampaignJoinLink, seed.link_id)
        link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        with pytest.raises(HTTPException) as error:
            _redeem(session, seed)
        assert error.value.status_code in (409, 410)
    state = _state(lane, seed)
    assert state.member_ids == []
    assert state.accepted_by_id is None


def test_expiry_does_not_break_safe_accepted_link_retry(lane):
    seed = _seed(lane)
    with Session(lane.observer) as session:
        _redeem(session, seed)
        member_id = _state(lane, seed).member_ids[0]
        link = session.get(CampaignJoinLink, seed.link_id)
        link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        _redeem(session, seed)
    assert _state(lane, seed).member_ids == [member_id]


def test_membership_and_token_consumption_roll_back_together(lane):
    seed = _seed(lane)

    class InterruptedCommit(RuntimeError):
        pass

    with Session(lane.observer) as session:
        original_commit = session.commit

        def fail_after_flush():
            # Exercise both SQL writes, then lose the transaction before commit.
            session.flush()
            raise InterruptedCommit("simulated disconnected request")

        session.commit = fail_after_flush
        try:
            with pytest.raises(InterruptedCommit):
                _redeem(session, seed)
        finally:
            session.commit = original_commit
            session.rollback()
    state = _state(lane, seed)
    assert state.status == "pending"
    assert state.accepted_by_id is None
    assert state.member_ids == []
    with Session(lane.observer) as session:
        _redeem(session, seed)
    assert len(_state(lane, seed).member_ids) == 1


@pytest.mark.parametrize("mismatch", ["issuer", "email", "missing_configuration"])
def test_new_recipient_requires_configured_issuer_and_exact_email(
    lane, monkeypatch, mismatch
):
    seed = _seed(lane)
    monkeypatch.setenv("GRIMOIRE_AUTH_ISSUER", ISSUER)
    with Session(lane.observer) as session:
        row = session.get(CampaignJoinLink, seed.link_id)
        row.recipient_id = None
        row.enrollment_allowed = True
        player = session.get(AppUser, seed.player_id)
        if mismatch == "issuer":
            player.issuer = "https://other.example/application/o/grimoire/"
        elif mismatch == "email":
            player.email = f"uninvited-{lane.identity}@example.test"
        else:
            monkeypatch.delenv("GRIMOIRE_AUTH_ISSUER")
        session.commit()
        with pytest.raises(HTTPException) as error:
            _redeem(session, seed)
        assert error.value.status_code == 403
    state = _state(lane, seed)
    assert state.status == "pending"
    assert state.recipient_id is None
    assert state.accepted_by_id is None
    assert state.member_ids == []


def test_new_recipient_is_bound_to_immutable_account_after_first_redemption(
    lane, monkeypatch
):
    seed = _seed(lane)
    monkeypatch.setenv("GRIMOIRE_AUTH_ISSUER", ISSUER)
    with Session(lane.observer) as session:
        row = session.get(CampaignJoinLink, seed.link_id)
        row.recipient_id = None
        row.enrollment_allowed = True
        session.commit()
        _redeem(session, seed)
        player = session.get(AppUser, seed.player_id)
        player.email = f"renamed-{lane.identity}@example.test"
        session.commit()
        stranger = session.get(AppUser, seed.stranger_id)
        stranger.email = seed.player_email
        session.commit()
        with pytest.raises(HTTPException) as error:
            _redeem(session, seed, seed.stranger_id)
        assert error.value.status_code == 403
        session.rollback()
        _redeem(session, seed)
    state = _state(lane, seed)
    assert state.status == "accepted"
    assert state.recipient_id == seed.player_id
    assert state.accepted_by_id == seed.player_id
    assert len(state.member_ids) == 1


def test_postgres_migration_rejects_invalid_terminal_states_and_duplicate_digest(lane):
    seed = _seed(lane)
    with lane.observer.connect() as connection:
        transaction = connection.begin()
        try:
            for status in ("unknown", "accepted"):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE grimoire.campaign_join_link SET status = :status "
                            "WHERE id = :id"
                        ),
                        {"id": seed.link_id, "status": status},
                    )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE grimoire.campaign_join_link "
                        "SET accepted_by_id = :player WHERE id = :id"
                    ),
                    {"id": seed.link_id, "player": seed.player_id},
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO grimoire.campaign_join_link "
                        "(campaign_id, recipient_id, invitee_email, issued_by_id, "
                        "token_digest, expires_at, enrollment_username) "
                        "SELECT campaign_id, recipient_id, invitee_email, issued_by_id, "
                        "token_digest, expires_at, enrollment_username "
                        "FROM grimoire.campaign_join_link WHERE id = :id"
                    ),
                    {"id": seed.link_id},
                )
        finally:
            transaction.rollback()


def test_join_link_table_is_not_granted_to_public_reader(lane):
    with lane.observer.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.table_privileges "
                    "WHERE table_schema = 'grimoire' "
                    "AND table_name = 'campaign_join_link' AND grantee = 'public_reader'"
                )
            )
            == 0
        )
