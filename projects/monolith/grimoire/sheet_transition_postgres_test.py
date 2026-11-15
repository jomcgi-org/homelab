"""Concurrent sheet route transitions against the migrated PostgreSQL schema."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from grimoire import router as routes
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    CharacterSheetVersion,
    Entity,
    PlayerCharacter,
)
from grimoire.sheets import CharacterSheetV1

WAIT_SECONDS = 10


@pytest.fixture
def lane(pg):
    identity = uuid4().hex
    engines = []

    def engine(actor):
        result = create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": f"sheet-{identity[:16]}-{actor}",
                "connect_timeout": WAIT_SECONDS,
                "options": "-c lock_timeout=15000 -c statement_timeout=20000",
            },
        )
        engines.append(result)
        return result

    observer = engine("observer")
    sheet = {
        "schema_version": 1,
        "ancestry": f"Human {identity}",
        "class_name": f"Fighter {identity}",
        "level": 5,
        "ability_scores": {
            "strength": 16,
            "dexterity": 14,
            "constitution": 14,
            "intelligence": 10,
            "wisdom": 12,
            "charisma": 8,
        },
    }
    with Session(observer) as session:
        dm = AppUser(email=f"sheet-dm-{identity}@example.test")
        player = AppUser(email=f"sheet-player-{identity}@example.test")
        campaign = Campaign(name=f"Sheet transitions {identity}")
        session.add_all([dm, player, campaign])
        session.flush()
        pc = PlayerCharacter(campaign_id=campaign.id, character_name="Elowen")
        session.add(pc)
        session.flush()
        session.add_all(
            [
                CampaignMember(campaign_id=campaign.id, app_user_id=dm.id, role="dm"),
                CampaignMember(
                    campaign_id=campaign.id,
                    app_user_id=player.id,
                    role="player",
                    player_character_id=pc.id,
                ),
                Entity(
                    entity_type="race",
                    name=sheet["ancestry"],
                    is_global=True,
                    detail={"speed": "30 feet"},
                ),
                Entity(
                    entity_type="class",
                    name=sheet["class_name"],
                    is_global=True,
                    detail={"hit_die": "d10", "saves": "Strength, Constitution"},
                ),
            ]
        )
        session.commit()
        draft = routes.create_character_sheet_draft(
            campaign.id,
            pc.id,
            CharacterSheetV1.model_validate(sheet),
            player.email,
            session,
        )
        result = SimpleNamespace(
            engine=engine,
            observer=observer,
            campaign_id=campaign.id,
            pc_id=pc.id,
            version_id=draft.id,
            dm_email=dm.email,
            player_email=player.email,
            sheet=sheet,
        )
    try:
        yield result
    finally:
        for value in engines:
            value.dispose()


def _transition(lane, session, operation, *, level=6):
    arguments = (lane.campaign_id, lane.pc_id, lane.version_id)
    if operation == "edit":
        return routes.update_character_sheet_draft(
            *arguments,
            routes.CharacterSheetDraftUpdate.model_validate(
                {**lane.sheet, "level": level}
            ),
            lane.player_email,
            session,
        )
    if operation == "submit":
        return routes.submit_character_sheet_draft(
            *arguments, lane.player_email, session
        )
    decide = (
        routes.approve_character_sheet
        if operation == "approve"
        else routes.return_character_sheet
    )
    return decide(
        *arguments,
        routes.SheetDecisionRequest(
            comment="Ready" if operation == "approve" else "Revise level"
        ),
        lane.dm_email,
        session,
    )


def _snapshot(lane):
    with Session(lane.observer) as observer:
        version = observer.get(CharacterSheetVersion, lane.version_id)
        pc = observer.get(PlayerCharacter, lane.pc_id)
        return deepcopy(version.model_dump()), deepcopy(pc.model_dump())


@pytest.mark.parametrize("operation", ["edit", "submit", "approve", "return"])
def test_stale_sheet_state_returns_conflict_without_changing_approval(lane, operation):
    if operation in ("approve", "return"):
        with Session(lane.observer) as session:
            _transition(lane, session, "submit")
    with Session(lane.engine("stale")) as stale:
        cached = stale.get(CharacterSheetVersion, lane.version_id)
        assert cached.status == (
            "draft" if operation in ("edit", "submit") else "submitted"
        )
        with Session(lane.engine("decision")) as current:
            if operation in ("edit", "submit"):
                _transition(lane, current, "submit")
            _transition(lane, current, "approve")
        before = _snapshot(lane)
        with pytest.raises(HTTPException) as caught:
            _transition(lane, stale, operation)
        assert caught.value.status_code == 409
        stale.rollback()
        assert _snapshot(lane) == before


def _wait_for_blocker(lane, waiter_pid, holder_pid, future):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with lane.observer.connect() as connection:
            blockers = connection.execute(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
            ).scalar_one()
        if holder_pid in blockers:
            return
        if future.done():
            future.result()
            pytest.fail(
                "The second sheet transition completed before the first released its row"
            )
        time.sleep(0.02)
    pytest.fail("The second sheet transition never waited on the first transition")


@pytest.mark.parametrize(
    ("first_operation", "second_operation"),
    [("edit", "submit"), ("approve", "return")],
)
def test_sheet_transitions_serialize_on_the_version_row(
    lane, monkeypatch, first_operation, second_operation
):
    if first_operation == "approve":
        with Session(lane.observer) as session:
            _transition(lane, session, "submit")
    first_read = Event()
    release_first = Event()
    second_started = Event()
    pids = {}
    original = routes._get_sheet_version_or_404

    def hold_first(session, *args, **kwargs):
        version = original(session, *args, **kwargs)
        if session.info.get("sheet_actor") == "first":
            first_read.set()
            assert release_first.wait(WAIT_SECONDS + 5)
        return version

    monkeypatch.setattr(routes, "_get_sheet_version_or_404", hold_first)

    def run(actor, operation):
        with Session(lane.engine(actor)) as session:
            session.info["sheet_actor"] = actor
            pids[actor] = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            if actor == "second":
                second_started.set()
            try:
                result = _transition(lane, session, operation)
                return result.status
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first", first_operation)
        try:
            assert first_read.wait(WAIT_SECONDS)
            second = pool.submit(run, "second", second_operation)
            assert second_started.wait(WAIT_SECONDS)
            _wait_for_blocker(lane, pids["second"], pids["first"], second)
        finally:
            release_first.set()
        # The route refreshes after commit, when the waiting submit may already
        # have completed. Both states describe a successful saved edit.
        first_status = first.result(timeout=WAIT_SECONDS)
        assert first_status in (
            ("draft", "submitted") if first_operation == "edit" else ("approved",)
        )
        assert second.result(timeout=WAIT_SECONDS) == (
            "submitted" if second_operation == "submit" else 409
        )
    version, pc = _snapshot(lane)
    if first_operation == "edit":
        assert version["status"] == "submitted"
        assert version["sheet"]["level"] == 6
        with Session(lane.observer) as session:
            _transition(lane, session, "approve")
        version, pc = _snapshot(lane)
        assert pc["sheet"] == version["sheet"]
    else:
        assert version["status"] == "approved"
        assert pc["sheet"] == version["sheet"]


def _edit_with_revision(lane, session, revision, **changes):
    return routes.update_character_sheet_draft(
        lane.campaign_id,
        lane.pc_id,
        lane.version_id,
        routes.CharacterSheetDraftUpdate.model_validate(
            {**lane.sheet, **changes, "expected_updated_at": revision}
        ),
        lane.player_email,
        session,
    )


@pytest.mark.parametrize("cached", [False, True])
def test_stale_draft_revision_preserves_the_other_tabs_changes(lane, cached):
    initial, _ = _snapshot(lane)
    with Session(lane.engine("stale-edit")) as stale:
        if cached:
            cached_version = stale.get(CharacterSheetVersion, lane.version_id)
            assert cached_version.updated_at == initial["updated_at"]
        with Session(lane.engine("winning-edit")) as winner:
            _edit_with_revision(
                lane,
                winner,
                initial["updated_at"],
                ability_scores={**lane.sheet["ability_scores"], "strength": 18},
            )
        before = _snapshot(lane)
        with pytest.raises(HTTPException) as caught:
            _edit_with_revision(lane, stale, initial["updated_at"], level=6)
        assert caught.value.status_code == 409
        assert caught.value.detail == (
            "This sheet changed since you opened it. "
            "Review the latest version before saving."
        )
        stale.rollback()
        assert _snapshot(lane) == before
        assert before[0]["sheet"]["ability_scores"]["strength"] == 18
        assert before[0]["sheet"]["level"] == 5


def test_concurrent_draft_edits_check_revision_after_the_row_lock(lane, monkeypatch):
    initial, _ = _snapshot(lane)
    first_read = Event()
    release_first = Event()
    second_started = Event()
    pids = {}
    original = routes._get_sheet_version_or_404

    def hold_first(session, *args, **kwargs):
        version = original(session, *args, **kwargs)
        if session.info.get("sheet_actor") == "first":
            first_read.set()
            assert release_first.wait(WAIT_SECONDS + 5)
        return version

    monkeypatch.setattr(routes, "_get_sheet_version_or_404", hold_first)

    def run(actor, level):
        with Session(lane.engine(actor)) as session:
            session.info["sheet_actor"] = actor
            pids[actor] = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            if actor == "second":
                second_started.set()
            try:
                _edit_with_revision(lane, session, initial["updated_at"], level=level)
                return 200
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first", 6)
        try:
            assert first_read.wait(WAIT_SECONDS)
            second = pool.submit(run, "second", 7)
            assert second_started.wait(WAIT_SECONDS)
            _wait_for_blocker(lane, pids["second"], pids["first"], second)
        finally:
            release_first.set()
        assert first.result(timeout=WAIT_SECONDS) == 200
        assert second.result(timeout=WAIT_SECONDS) == 409
    version, _ = _snapshot(lane)
    assert version["sheet"]["level"] == 6
    assert version["updated_at"] > initial["updated_at"]
    assert "expected_updated_at" not in version["sheet"]


def test_draft_edit_without_revision_remains_backward_compatible(lane):
    with Session(lane.observer) as session:
        edited = _edit_with_revision(lane, session, None, level=6)
        assert edited.sheet == {**lane.sheet, "level": 6}


def test_draft_revision_advances_when_clock_does_not(lane, monkeypatch):
    initial, _ = _snapshot(lane)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return initial["updated_at"].astimezone(tz or timezone.utc)

    monkeypatch.setattr(routes, "datetime", FrozenDatetime)
    with Session(lane.observer) as session:
        saved = _edit_with_revision(lane, session, initial["updated_at"], level=6)
        assert saved.updated_at > initial["updated_at"]
