"""Storage and HTTP boundary guards, pinned independently of route policy."""

from datetime import datetime
from typing import get_args
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, event, null
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.audience import Audience
from grimoire.models import (
    Campaign,
    EventKind,
    GameSession,
    PlayerCharacter,
    SessionEvent,
)
from grimoire.session_events import (
    InvalidEventAudienceError,
    InvalidEventKindError,
    SessionEndedError,
    append_event,
    play_enabled,
    require_play_enabled,
)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'events.db'}",
        execution_options={"schema_translate_map": {"grimoire": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def game_session(session):
    campaign = Campaign(name="Event test")
    session.add(campaign)
    session.flush()
    row = GameSession(campaign_id=campaign.id)
    session.add(row)
    session.commit()
    return row


def _append(session, game_session, **changes):
    args = {
        "game_session": game_session,
        "kind": "action",
        "audience": Audience("table"),
        "author_member_id": None,
        "body": {},
    }
    args.update(changes)
    return append_event(session, **args)


def test_literal_kinds_and_all_kinds_append(session, game_session):
    kinds = (
        "narration",
        "action",
        "roll",
        "reveal",
        "handout",
        "turn",
        "system",
        "utterance",
    )
    assert get_args(EventKind) == kinds
    for seq, kind in enumerate(kinds, start=1):
        row = _append(session, game_session, kind=kind, body={"kind": kind})
        assert row.seq == seq
        assert row.kind == kind
        assert row.body == {"kind": kind}
        assert row.campaign_id == game_session.campaign_id
        assert row.session_id == game_session.id
        assert row.audience == "table"
        assert row.audience_pc_ids == []
        assert row.author_member_id is None
        assert row.retracted_at is None
        assert isinstance(row.created_at, datetime)
    assert isinstance(SessionEvent.__table__.c.seq.type, BigInteger)


@pytest.mark.parametrize("status", ["active", "paused"])
def test_active_and_paused_allow_append(session, game_session, status):
    game_session.status = status
    session.commit()
    assert _append(session, game_session).seq == 1


def test_ended_refused(session, game_session):
    game_session.status = "ended"
    session.commit()
    with pytest.raises(SessionEndedError, match="ended"):
        _append(session, game_session)
    assert session.exec(select(SessionEvent)).all() == []


def test_stale_ended_row_refused(engine, session, game_session):
    session_id = game_session.id
    with Session(engine) as other:
        row = other.get(GameSession, session_id)
        row.status = "ended"
        other.commit()
    assert game_session.status == "active"
    with pytest.raises(SessionEndedError):
        _append(session, game_session)


def test_unknown_kind_refused(session, game_session):
    with pytest.raises(InvalidEventKindError, match="Unknown event kind"):
        _append(session, game_session, kind="unknown")
    assert session.exec(select(SessionEvent)).all() == []


def test_nonexistent_session_refused(session, game_session):
    missing = GameSession(campaign_id=game_session.campaign_id)
    with pytest.raises(ValueError, match="does not exist"):
        _append(session, missing)


def test_pcs_campaign_scope_and_sorted_ids(session, game_session):
    other = Campaign(name="Other")
    session.add(other)
    session.flush()
    local = PlayerCharacter(
        campaign_id=game_session.campaign_id, character_name="Local"
    )
    second = PlayerCharacter(
        campaign_id=game_session.campaign_id, character_name="Second"
    )
    foreign = PlayerCharacter(campaign_id=other.id, character_name="Foreign")
    session.add_all([local, second, foreign])
    session.flush()
    for ids in [(foreign.id,), (local.id, foreign.id), (str(uuid4()),)]:
        with pytest.raises(InvalidEventAudienceError, match="belong to this campaign"):
            _append(session, game_session, audience=Audience("pcs", frozenset(ids)))
    assert session.exec(select(SessionEvent)).all() == []
    row = _append(
        session,
        game_session,
        audience=Audience("pcs", frozenset([second.id, local.id])),
    )
    assert row.audience == "pcs"
    assert row.audience_pc_ids == sorted([local.id, second.id])
    assert row.seq == 1


def test_conflicting_author_provenance_refused(session, game_session):
    with pytest.raises(
        InvalidEventAudienceError, match="Conflicting author provenance"
    ):
        _append(
            session,
            game_session,
            audience=Audience("dm", author_member_id=str(uuid4())),
        )


def test_sequence_is_per_session_and_rollback_has_no_gap(session, game_session):
    assert _append(session, game_session).seq == 1
    session.commit()
    assert _append(session, game_session).seq == 2
    session.rollback()
    assert _append(session, game_session).seq == 2
    other = GameSession(campaign_id=game_session.campaign_id, status="ended")
    session.add(other)
    session.flush()
    other.status = "active"
    assert _append(session, other).seq == 1
    session.commit()
    assert [
        row.seq
        for row in session.exec(
            select(SessionEvent)
            .where(SessionEvent.session_id == game_session.id)
            .order_by(SessionEvent.seq)
        )
    ] == [1, 2]


def test_append_never_commits(session, game_session, monkeypatch):
    def no_commit():
        pytest.fail("append_event must leave transaction ownership to the caller")

    monkeypatch.setattr(session, "commit", no_commit)
    row_id = _append(session, game_session).id
    session.rollback()
    assert session.get(SessionEvent, row_id) is None


def test_lock_is_for_update_and_refreshes_identity_map(
    session, game_session, monkeypatch
):
    original_exec = session.exec
    statements = []

    def capture(statement, *args, **kwargs):
        statements.append(statement)
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", capture)
    _append(session, game_session)
    assert str(statements[0].compile(dialect=postgresql.dialect())).endswith(
        "FOR UPDATE"
    )
    assert statements[0].get_execution_options()["populate_existing"] is True


def test_integrity_error_not_hidden_or_retried(session, game_session, monkeypatch):
    calls = []
    failure = IntegrityError("insert", {}, Exception("missing serialization"))
    original_flush = session.flush

    def broken_flush(*args, **kwargs):
        if not any(isinstance(row, SessionEvent) for row in session.new):
            return original_flush(*args, **kwargs)
        calls.append(1)
        raise failure

    monkeypatch.setattr(session, "flush", broken_flush)
    with pytest.raises(IntegrityError) as caught:
        _append(session, game_session)
    assert caught.value is failure
    assert calls == [1]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("false", False),
        ("true", True),
        ("junk", False),
        ("TRUE", False),
        (" true ", False),
        ("1", False),
        ("", False),
    ],
)
def test_play_flag_exact_literal(monkeypatch, value, expected):
    monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", value)
    assert play_enabled() is expected


def test_play_flag_reads_at_call_time(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    assert play_enabled() is True
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    assert play_enabled() is False


def test_dependency_404_and_enabled(monkeypatch):
    monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED", raising=False)
    with pytest.raises(HTTPException) as caught:
        require_play_enabled()
    assert caught.value.status_code == 404
    assert caught.value.detail == "Not found"
    app = FastAPI()

    @app.get("/play", dependencies=[Depends(require_play_enabled)])
    def play():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/play").status_code == 404
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
        assert require_play_enabled() is None
        assert client.get("/play").status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"audience": "unknown"},
        {"seq": 0},
        {"seq": -1},
        {"audience": "pcs", "audience_pc_ids": []},
        {"audience": "table", "audience_pc_ids": ["pc"]},
        {"audience": "dm", "audience_pc_ids": ["pc"]},
        {"audience_pc_ids": {}},
        {"audience_pc_ids": None},
        {"body": null()},
    ],
)
def test_model_checks_reject_invalid_storage(session, game_session, changes):
    values = {
        "campaign_id": game_session.campaign_id,
        "session_id": game_session.id,
        "seq": 1,
        "kind": "action",
        "audience": "table",
    }
    values.update(changes)
    session.add(SessionEvent(**values))
    with pytest.raises(IntegrityError):
        session.flush()


def test_model_default_values_are_independent_and_unique_seq(session, game_session):
    values = {
        "campaign_id": game_session.campaign_id,
        "session_id": game_session.id,
        "seq": 1,
        "kind": "action",
        "audience": "dm",
    }
    first = SessionEvent(**values)
    second = SessionEvent(**values)
    assert first.body == {}
    assert first.audience_pc_ids == []
    assert first.body is not second.body
    assert first.audience_pc_ids is not second.audience_pc_ids
    assert first.id != second.id
    session.add_all([first, second])
    with pytest.raises(IntegrityError):
        session.flush()
