"""PostgreSQL interleaving regression for character seating routes."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire.models import AppUser, Campaign, CampaignMember, PlayerCharacter
from grimoire.router import (
    CharacterNameRequest,
    MemberCharacterRequest,
    assign_member_character,
    create_own_character,
)

WAIT_SECONDS = 10


@pytest.fixture
def lane(pg):
    identity = uuid4().hex
    engines = []

    def engine(actor):
        value = create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": f"char-assign-{identity}-{actor}",
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


@contextmanager
def _session(engine):
    with Session(engine) as session:
        yield session


def _seed(lane):
    identity = lane.identity
    with _session(lane.observer) as session:
        dm_user = AppUser(email=f"dm-{identity}@example.test")
        player_user = AppUser(email=f"player-{identity}@example.test")
        session.add(dm_user)
        session.add(player_user)
        session.flush()
        campaign = Campaign(
            name=f"Deadlock campaign {identity}",
            owner_app_user_id=dm_user.id,
        )
        session.add(campaign)
        session.flush()
        dm_member = CampaignMember(
            campaign_id=campaign.id,
            app_user_id=dm_user.id,
            role="dm",
        )
        player_member = CampaignMember(
            campaign_id=campaign.id,
            app_user_id=player_user.id,
            role="player",
        )
        session.add(dm_member)
        session.add(player_member)
        session.commit()
        return SimpleNamespace(
            campaign_id=campaign.id,
            dm_email=dm_user.email,
            player_email=player_user.email,
            player_member_id=player_member.id,
        )


# Postgres renders schema-qualified table names (the SQLite harness strips
# schemas), and "campaign" is a prefix of "campaign_member", so match the
# FROM clause with a word boundary.
_CAMPAIGN_FROM = re.compile(r"FROM\s+grimoire\.campaign(?!\w)")
_MEMBER_FROM = re.compile(r"FROM\s+grimoire\.campaign_member(?!\w)")


def _is_campaign_lock(sql: str) -> bool:
    return bool(_CAMPAIGN_FROM.search(sql))


def _is_member_lock(sql: str) -> bool:
    return bool(_MEMBER_FROM.search(sql))


def test_interleaved_assign_and_self_create_serialize_without_deadlock(lane):
    seed = _seed(lane)
    self_member_locked = Event()
    dm_campaign_locked = Event()

    def self_create():
        with _session(lane.engine("self-create")) as session:
            original_exec = session.exec

            def paused_exec(statement, *args, **kwargs):
                result = original_exec(statement, *args, **kwargs)
                sql = str(statement.compile(dialect=postgresql.dialect()))
                # Pause only after the locking read: the earlier membership
                # lookup touches the same table without holding the row.
                if (
                    _is_member_lock(sql)
                    and "FOR UPDATE" in sql
                    and not self_member_locked.is_set()
                ):
                    self_member_locked.set()
                    assert dm_campaign_locked.wait(WAIT_SECONDS), (
                        "DM assign never took the campaign lock"
                    )
                return result

            session.exec = paused_exec
            try:
                return create_own_character(
                    seed.campaign_id,
                    CharacterNameRequest(name="Self PC"),
                    seed.player_email,
                    session,
                )
            finally:
                session.exec = original_exec

    def dm_assign():
        assert self_member_locked.wait(WAIT_SECONDS), (
            "self-create never took the member lock"
        )
        with _session(lane.engine("dm-assign")) as session:
            original_exec = session.exec

            def signal_exec(statement, *args, **kwargs):
                result = original_exec(statement, *args, **kwargs)
                sql = str(statement.compile(dialect=postgresql.dialect()))
                if _is_campaign_lock(sql) and not dm_campaign_locked.is_set():
                    dm_campaign_locked.set()
                return result

            session.exec = signal_exec
            try:
                return assign_member_character(
                    seed.campaign_id,
                    seed.player_member_id,
                    MemberCharacterRequest(new=CharacterNameRequest(name="DM PC")),
                    seed.dm_email,
                    session,
                )
            finally:
                session.exec = original_exec

    with ThreadPoolExecutor(max_workers=2) as pool:
        self_future = pool.submit(self_create)
        dm_future = pool.submit(dm_assign)
        self_character = self_future.result(timeout=WAIT_SECONDS + 20)
        dm_view = dm_future.result(timeout=WAIT_SECONDS + 20)

    assert isinstance(self_character, PlayerCharacter)
    assert dm_view.player_character_id is not None
    assert dm_view.player_character_id != self_character.id

    with _session(lane.observer) as session:
        assert session.get(PlayerCharacter, self_character.id) is not None
        assert session.get(PlayerCharacter, dm_view.player_character_id) is not None
        member = session.get(CampaignMember, seed.player_member_id)
        assert member.player_character_id == dm_view.player_character_id
        remaining = session.exec(
            select(CampaignMember).where(
                CampaignMember.player_character_id == self_character.id
            )
        ).all()
        assert remaining == []
