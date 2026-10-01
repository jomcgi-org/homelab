"""Storage and HTTP boundary guards, pinned independently of route policy."""

from datetime import datetime, timedelta, timezone
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
from grimoire.testing.leak_harness import ROLES, sqlite_harness


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


@pytest.fixture
def http_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "http-events.db") as h:
        h.prepare("play")
        with TestClient(h.app()) as client:
            yield h, client


def _url(h, suffix="", *, campaign=None, game_session=None):
    return (
        f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}/sessions"
        + (f"/{game_session or h.rows['campaign_session'].id}" if suffix else "")
        + suffix
    )


def _post(h, client, viewer="dm", **changes):
    body = {"kind": "action", "audience": "table", "body": {"text": "Move"}}
    body.update(changes)
    return client.post(_url(h, "/events"), headers=h.headers(viewer), json=body)


PLAY_ROUTES = (
    ("GET", "list"),
    ("GET", "current"),
    ("POST", "append"),
    ("GET", "poll"),
    ("POST", "retract"),
)


@pytest.mark.parametrize("method,route", PLAY_ROUTES)
@pytest.mark.parametrize("viewer", (*ROLES, "anonymous"))
@pytest.mark.parametrize("flag", (None, "false"))
def test_all_play_routes_flag_off_before_auth(
    http_harness, monkeypatch, method, route, viewer, flag
):
    h, client = http_harness
    if flag is None:
        monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED")
    else:
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", flag)
    paths = {
        "list": _url(h),
        "current": _url(h) + "/current",
        "append": _url(h, "/events"),
        "poll": _url(h, "/events"),
        "retract": _url(h, f"/events/{h.rows['event_table'].id}/retract"),
    }
    before = h.snapshot()
    response = client.request(
        method,
        paths[route],
        headers={} if viewer == "anonymous" else h.headers(viewer),
        json={"kind": "action", "audience": "table", "body": {}},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}
    assert h.snapshot() == before


def test_existing_session_writes_work_with_flag_off(http_harness, monkeypatch):
    h, client = http_harness
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    response = client.patch(
        _url(h, "") + f"/{h.rows['campaign_session'].id}",
        headers=h.headers("dm"),
        json={"status": "ended"},
    )
    assert response.status_code == 200
    response = client.post(_url(h), headers=h.headers("dm"))
    assert response.status_code == 200
    assert response.json()["status"] == "active"


@pytest.mark.parametrize("viewer", ("dm", "player_a", "player_b", "no_character"))
def test_session_list_newest_and_current_active_or_paused(http_harness, viewer):
    h, client = http_harness
    current = h.rows["campaign_session"]
    older = GameSession(
        campaign_id=current.campaign_id,
        status="ended",
        started_at=current.started_at - timedelta(days=1),
    )
    h.session.add(older)
    h.session.commit()
    response = client.get(_url(h), headers=h.headers(viewer))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [current.id, older.id]
    for status in ("active", "paused"):
        current.status = status
        h.session.commit()
        response = client.get(_url(h) + "/current", headers=h.headers(viewer))
        assert response.status_code == 200
        assert response.json()["id"] == current.id
        assert response.json()["status"] == status
    current.status = "ended"
    h.session.commit()
    assert (
        client.get(_url(h) + "/current", headers=h.headers(viewer)).status_code == 404
    )


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
@pytest.mark.parametrize(
    "kind", ("narration", "reveal", "roll", "handout", "turn", "system", "utterance")
)
def test_player_forbidden_kinds(http_harness, viewer, kind):
    h, client = http_harness
    before = h.snapshot()
    response = _post(h, client, viewer, kind=kind)
    assert response.status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
def test_player_cannot_post_pcs_audience(http_harness, viewer):
    h, client = http_harness
    before = h.snapshot()
    assert (
        _post(
            h,
            client,
            viewer,
            audience="pcs",
            audience_pc_ids=[h.rows["character_a"].id],
        ).status_code
        == 403
    )
    assert h.snapshot() == before


def test_dm_utterance_ingest_only(http_harness):
    h, client = http_harness
    before = h.snapshot()
    assert _post(h, client, kind="utterance").status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "viewer,member_key",
    (
        ("dm", "member_dm"),
        ("player_a", "member_player_a"),
        ("player_b", "member"),
        ("no_character", "member_no_character"),
    ),
)
@pytest.mark.parametrize("audience", ("dm", "table"))
def test_allowed_actions_have_authenticated_author(
    http_harness, viewer, member_key, audience
):
    h, client = http_harness
    response = _post(h, client, viewer, audience=audience)
    assert response.status_code == 200, response.text
    assert response.json()["seq"] == 9
    assert response.json()["audience_pc_ids"] == []
    row = h.session.get(SessionEvent, response.json()["id"])
    assert row.author_member_id == h.rows[member_key].id
    assert row.campaign_id == h.rows["campaign"].id
    assert row.session_id == h.rows["campaign_session"].id
    assert row.kind == "action"
    assert row.body == {"text": "Move"}


@pytest.mark.parametrize(
    "kind", ("narration", "action", "roll", "reveal", "handout", "turn", "system")
)
@pytest.mark.parametrize("audience", ("dm", "table", "pcs"))
def test_dm_can_post_all_non_ingest_kinds_and_audiences(http_harness, kind, audience):
    h, client = http_harness
    ids = [h.rows["character_a"].id] if audience == "pcs" else []
    response = _post(h, client, kind=kind, audience=audience, audience_pc_ids=ids)
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == kind
    assert response.json()["audience"] == audience


@pytest.mark.parametrize("viewer", ("dm", "player_a"))
def test_ended_session_refuses_append(http_harness, viewer):
    h, client = http_harness
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    assert _post(h, client, viewer).status_code == 409
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "changes",
    (
        {"audience": "unknown"},
        {"audience": "pcs", "audience_pc_ids": []},
        {"audience": "pcs", "audience_pc_ids": ["not-a-uuid"]},
        {"audience": "table", "audience_pc_ids": [str(uuid4())]},
        {"audience": "dm", "audience_pc_ids": [str(uuid4())]},
        {"audience": "pcs", "audience_pc_ids": [str(uuid4())]},
        {"kind": "unknown"},
        {"author_member_id": str(uuid4())},
        {"body": None},
    ),
)
def test_invalid_event_request_is_422_without_writes(http_harness, changes):
    h, client = http_harness
    before = h.snapshot()
    assert _post(h, client, **changes).status_code == 422
    assert h.snapshot() == before


def test_foreign_and_mixed_pc_audiences_are_422(http_harness):
    h, client = http_harness
    before = h.snapshot()
    for ids in (
        [h.rows["other_character"].id],
        [h.rows["character_a"].id, h.rows["other_character"].id],
    ):
        assert _post(h, client, audience="pcs", audience_pc_ids=ids).status_code == 422
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "viewer,keys",
    (
        (
            "player_a",
            (
                "event_character_a",
                "event_character_a_retracted",
                "event_table",
                "event_table_retracted",
            ),
        ),
        (
            "player_b",
            (
                "event_character",
                "event_character_retracted",
                "event_table",
                "event_table_retracted",
            ),
        ),
        ("no_character", ("event_table", "event_table_retracted")),
    ),
)
@pytest.mark.parametrize("limit", (1, 2))
def test_advancing_polls_never_leak_restricted_or_retracted_events(
    http_harness, viewer, keys, limit
):
    h, client = http_harness
    seen = []
    after = 0
    for _ in range(6):
        response = client.get(
            _url(h, "/events"),
            headers=h.headers(viewer),
            params={"after": after, "limit": limit},
        )
        assert response.status_code == 200
        h.assert_no_leak(response, viewer)
        rows = response.json()
        if not rows:
            break
        assert all(row["seq"] > after for row in rows)
        assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
        seen.extend(row["id"] for row in rows)
        after = rows[-1]["seq"]
    assert seen == [h.rows[key].id for key in keys]


def test_dm_narration_for_pc_a_only_canary(http_harness):
    h, client = http_harness
    token = h.token("http.narration.body", ("dm", "player_a"))
    response = _post(
        h,
        client,
        kind="narration",
        audience="pcs",
        audience_pc_ids=[h.rows["character_a"].id],
        body={"secret": token},
    )
    assert response.status_code == 200
    event_id = response.json()["id"]
    for viewer in ROLES:
        response = client.get(_url(h, "/events"), headers=h.headers(viewer))
        h.assert_no_leak(response, viewer)
        if viewer in ("outsider", "other_campaign"):
            assert response.status_code == 404
        else:
            assert response.status_code == 200
            assert (event_id in [row["id"] for row in response.json()]) == (
                viewer in ("dm", "player_a")
            )
            assert (token in response.text) == (viewer in ("dm", "player_a"))


def test_poll_filters_in_one_sql_query_before_limit(http_harness):
    h, client = http_harness
    statements = []
    connection = h.session.connection()

    def capture(conn, cursor, statement, parameters, context, many):
        if "FROM session_event" in statement:
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        response = client.get(
            _url(h, "/events"), headers=h.headers("player_a"), params={"limit": 1}
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [h.rows["event_character_a"].id]
    assert len(statements) == 1
    assert "json_each" in statements[0]
    assert "session_event.seq >" in statements[0]
    assert "ORDER BY session_event.seq" in statements[0]
    assert "LIMIT" in statements[0]


def test_poll_literal_default_maximum_and_strict_after(http_harness):
    h, client = http_harness
    h.session.add_all(
        [
            SessionEvent(
                campaign_id=h.rows["campaign"].id,
                session_id=h.rows["campaign_session"].id,
                seq=seq,
                kind="system",
                audience="table",
                body={},
            )
            for seq in range(9, 510)
        ]
    )
    h.session.commit()

    def poll(**params):
        return client.get(_url(h, "/events"), headers=h.headers("dm"), params=params)

    rows = poll().json()
    assert len(rows) == 100
    assert [row["seq"] for row in rows] == list(range(1, 101))
    assert len(poll(limit=500).json()) == 500
    assert poll(limit=501).status_code == 422
    assert poll(limit=0).status_code == 422
    assert poll(limit=-1).status_code == 422
    assert poll(after=-1).status_code == 422
    assert poll(after="bad").status_code == 422
    assert [row["seq"] for row in poll(after=100, limit=1).json()] == [101]
    assert poll(after=509).json() == []


def test_retraction_permissions_body_projection_and_repeat(http_harness):
    h, client = http_harness
    response = _post(h, client, "player_a")
    own_id = response.json()["id"]
    own_url = _url(h, f"/events/{own_id}/retract")
    before = h.snapshot()
    assert client.post(own_url, headers=h.headers("player_b")).status_code == 403
    for key in ("event_table", "event_dm"):
        assert (
            client.post(
                _url(h, f"/events/{h.rows[key].id}/retract"),
                headers=h.headers("player_a"),
            ).status_code
            == 403
        )
    assert h.snapshot() == before
    response = client.post(own_url, headers=h.headers("player_a"))
    assert response.status_code == 200
    assert response.json()["body"] is None
    timestamp = response.json()["retracted_at"]
    assert timestamp.endswith("Z")
    before = h.snapshot()
    repeated = client.post(own_url, headers=h.headers("player_a"))
    assert repeated.status_code == 200
    assert repeated.json()["retracted_at"] == timestamp
    assert h.snapshot() == before
    for viewer in ("dm", "player_a", "player_b", "no_character"):
        response = client.get(_url(h, "/events"), headers=h.headers(viewer))
        row = next(row for row in response.json() if row["id"] == own_id)
        assert row["retracted_at"] == timestamp
        assert row["body"] == ({"text": "Move"} if viewer == "dm" else None)
    response = client.post(own_url, headers=h.headers("dm"))
    assert response.status_code == 200
    assert response.json()["body"] == {"text": "Move"}
    assert response.json()["retracted_at"] == timestamp


@pytest.mark.parametrize("viewer", ("dm", "player_a"))
def test_retraction_allowed_after_session_ends(http_harness, viewer):
    h, client = http_harness
    event_id = _post(h, client, "player_a").json()["id"]
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    response = client.post(
        _url(h, f"/events/{event_id}/retract"), headers=h.headers(viewer)
    )
    assert response.status_code == 200
    assert response.json()["retracted_at"] is not None


@pytest.mark.parametrize(
    "method,suffix",
    (("GET", "/events"), ("POST", "/events"), ("POST", "/events/replaced/retract")),
)
def test_session_and_event_scope_are_404(http_harness, method, suffix):
    h, client = http_harness
    suffix = suffix.replace("replaced", h.rows["event_table"].id)
    before = h.snapshot()
    for session_id in (h.rows["other_session"].id, str(uuid4())):
        response = client.request(
            method,
            _url(h, suffix, game_session=session_id),
            headers=h.headers("dm"),
            json={"kind": "action", "audience": "table", "body": {}},
        )
        assert response.status_code == 404
    # An event from this campaign but another session must also be hidden.
    second = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    h.session.add(second)
    h.session.commit()
    if suffix.endswith("/retract"):
        assert (
            client.post(
                _url(h, suffix, game_session=second.id), headers=h.headers("dm")
            ).status_code
            == 404
        )
        assert (
            client.post(
                _url(h, f"/events/{uuid4()}/retract"), headers=h.headers("dm")
            ).status_code
            == 404
        )
    h.session.delete(second)
    h.session.commit()
    assert h.snapshot() == before


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
