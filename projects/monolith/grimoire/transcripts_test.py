"""Consent privacy, state transitions and the default-off HTTP boundary."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from grimoire.models import Campaign, GameSession, SessionEvent, TranscriptConsent
from grimoire.router import _lock_consent_member
from grimoire.session_events import require_transcript_enabled, transcript_enabled
from grimoire.testing.leak_harness import sqlite_harness

PREFIX = "/api/grimoire/campaigns/{campaign_id}"
CONSENT = PREFIX + "/transcript/consent"
STATE = PREFIX + "/sessions/{session_id}/transcript"
UTTERANCES = PREFIX + "/sessions/{session_id}/utterances"
EXPECTED_ROUTES = {
    ("POST", UTTERANCES),
    ("PUT", CONSENT),
    ("DELETE", CONSENT),
    ("GET", CONSENT),
    ("GET", STATE),
    ("PUT", STATE),
}


@pytest.fixture
def http_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", "true")
    with sqlite_harness(tmp_path / "transcripts.db") as h:
        h.prepare("play")
        with TestClient(h.app()) as client:
            yield h, client


def url(h, *, state=False, campaign=None, game_session=None):
    path = STATE if state else CONSENT
    return path.format(
        campaign_id=campaign or h.rows["campaign"].id,
        session_id=game_session or h.rows["campaign_session"].id,
    )


def grant(h, client, viewer="player_a", processor="Local STT"):
    return client.put(url(h), headers=h.headers(viewer), json={"processor": processor})


def state(h, client, value, viewer="dm"):
    return client.put(
        url(h, state=True), headers=h.headers(viewer), json={"state": value}
    )


def utterance_body(**changes):
    return {
        "text": "A fresh spoken clue",
        "source": "browser",
        "started_at": "2026-10-10T12:00:00Z",
        "ended_at": "2026-10-10T12:00:01Z",
        "confidence": 0.9,
        **changes,
    }


def ingest(h, client, viewer="player_a", *, session_id=None, **changes):
    response = client.post(
        UTTERANCES.format(
            campaign_id=h.rows["campaign"].id,
            session_id=session_id or h.rows["campaign_session"].id,
        ),
        headers=h.headers(viewer),
        json=utterance_body(**changes),
    )
    h.assert_no_leak(response, viewer)
    return response


@pytest.mark.parametrize("revoked", (False, True))
def test_ingest_requires_active_speaker_consent_including_after_revoke(http_harness, revoked):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    if not revoked:
        h.session.delete(h.rows["consent_player_a"])
        h.session.commit()
    else:
        assert client.delete(url(h), headers=h.headers("player_a")).status_code == 204
    before = h.snapshot()
    assert ingest(h, client).status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize("missing", ("dm", "operator", "player_a", "player_b", "no_character"))
def test_null_table_speaker_requires_consent_from_every_current_member(http_harness, missing):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    consent = h.rows[f"consent_{missing}"]
    h.session.delete(consent)
    h.session.commit()
    before = h.snapshot()
    assert ingest(h, client, "dm", source="table", speaker_label="Table mic").status_code == 403
    assert h.snapshot() == before
    # Self-service grant restores the member's consent, not an adapter override.
    assert grant(h, client, missing).status_code == 200
    response = ingest(h, client, "dm", source="table", speaker_label="Table mic")
    assert response.status_code == 200, response.text
    assert response.json()["author_member_id"] is None
    assert response.json()["body"]["speaker_label"] == "Table mic"


@pytest.mark.parametrize("changes", (
    {"source": "table", "speaker_label": "Table"},
    {"source": "discord"},
    {"audience": {"kind": "pcs", "pc_ids": []}},
))
def test_player_cannot_ingest_adapter_sources_or_pc_audience(http_harness, changes):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    before = h.snapshot()
    assert ingest(h, client, **changes).status_code == 403
    assert h.snapshot() == before


def test_browser_cannot_claim_another_member_as_speaker(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    before = h.snapshot()
    assert ingest(h, client, speaker_member_id=h.rows["member"].id).status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize("explicit_speaker", (False, True))
def test_browser_attribution_defaults_to_caller_and_accepts_own_id(http_harness, explicit_speaker):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    changes = {"speaker_member_id": h.rows["member_player_a"].id} if explicit_speaker else {}
    response = ingest(h, client, **changes)
    assert response.status_code == 200, response.text
    assert response.json()["author_member_id"] == h.rows["member_player_a"].id
    row = h.session.get(SessionEvent, response.json()["id"])
    assert row.author_member_id == h.rows["member_player_a"].id
    assert row.audience == "table"
    assert row.body == {
        "text": "A fresh spoken clue", "source": "browser", "confidence": 0.9,
        "started_at": "2026-10-10T12:00:00+00:00", "ended_at": "2026-10-10T12:00:01+00:00",
    }


@pytest.mark.parametrize("source", ("table", "discord"))
def test_dm_adapter_attribution_is_campaign_scoped_and_response_is_projected(http_harness, source):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    for speaker in (h.rows["member_other_campaign"].id, str(uuid4()), "not-a-uuid"):
        before = h.snapshot()
        assert ingest(h, client, "dm", source=source, speaker_member_id=speaker).status_code == 403
        assert h.snapshot() == before
    response = ingest(h, client, "dm", source=source, speaker_member_id=h.rows["member_player_a"].id)
    assert response.status_code == 200, response.text
    assert response.json()["author_member_id"] == h.rows["member_player_a"].id
    assert set(response.json()["body"]) == {"text", "started_at", "ended_at", "source", "confidence"}
    assert response.json()["audience"] == "table"
    assert response.json()["kind"] == "utterance"
    for value in response.json()["body"].values():
        assert value != h.rows["member_player_a"].id


@pytest.mark.parametrize("previous", ("off", "paused"))
def test_ingest_refuses_off_or_paused_and_accepts_after_dm_resume(http_harness, previous):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    if previous == "paused":
        assert state(h, client, "paused", "player_a").status_code == 200
        row = events(h)[-1]
        assert row.body == {"transcript_state": "paused", "previous": "on"}
        assert row.audience == "table"
        assert row.author_member_id == h.rows["member_player_a"].id
    else:
        assert state(h, client, "off").status_code == 200
    # State failure precedes missing-consent failure.
    assert client.delete(url(h), headers=h.headers("player_a")).status_code == 204
    before = h.snapshot()
    response = ingest(h, client)
    assert response.status_code == 409
    assert previous in response.json()["detail"]
    assert h.snapshot() == before
    assert grant(h, client).status_code == 200
    assert state(h, client, "on").status_code == 200
    assert ingest(h, client).status_code == 200


def test_browser_dm_whisper_is_visible_only_to_speaker_and_dm_on_real_feed(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    text = h.token("ingested_whisper.text", ("dm", "player_a"))
    response = ingest(h, client, text=text, audience={"kind": "dm"})
    assert response.status_code == 200, response.text
    whisper_id = response.json()["id"]
    assert response.json()["author_member_id"] == h.rows["member_player_a"].id
    base = PREFIX.format(campaign_id=h.rows["campaign"].id)
    feed = base + f"/sessions/{h.rows['campaign_session'].id}/events"
    for viewer in ("dm", "player_a", "player_b", "no_character", "outsider", "other_campaign"):
        result = client.get(feed, headers=h.headers(viewer))
        h.assert_no_leak(result, viewer)
        if viewer in ("outsider", "other_campaign"):
            assert result.status_code == 404
            continue
        assert result.status_code == 200, result.text
        matches = [event for event in result.json() if event["id"] == whisper_id]
        assert bool(matches) is (viewer in ("dm", "player_a"))
        if matches:
            assert matches[0]["body"]["text"] == text
        for path in (base + "/journal", base + f"/sessions/{h.rows['campaign_session'].id}/journal", base + "/sessions/current"):
            journal = client.get(path, headers=h.headers(viewer))
            assert journal.status_code == 200, journal.text
            h.assert_no_leak(journal, viewer)


def test_ingest_request_id_retry_is_same_event_and_changed_payload_conflicts(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    request_id = str(uuid4())
    first = ingest(h, client, request_id=request_id)
    assert first.status_code == 200, first.text
    before = h.snapshot()
    assert ingest(h, client, request_id=request_id).json() == first.json()
    assert h.snapshot() == before
    assert ingest(h, client, request_id=request_id, text="Different text").status_code == 409
    assert h.snapshot() == before


@pytest.mark.parametrize("changes", (
    {"audio": "raw bytes"}, {"unknown": "payload"},
    {"text": ""}, {"text": "x" * 4001}, {"speaker_label": "x" * 81},
    {"confidence": -0.1}, {"confidence": 1.1},
    {"started_at": "2026-10-10T12:00:00"},
    {"ended_at": "2026-10-10T12:00:01"},
    {"ended_at": "2026-10-10T11:59:59Z"}, {"source": "microphone"},
    {"audience": {"kind": "table", "audio": "raw bytes"}},
    {"audience": {"kind": "table", "pc_ids": ["invalid"]}},
    {"audience": {"kind": "pcs", "pc_ids": []}},
    {"audience": {"kind": "pcs", "pc_ids": ["invalid"]}},
    {"source": "table"}, {"source": "table", "speaker_label": ""},
    {"source": "discord", "speaker_label": "Unattributed"},
))
def test_ingest_is_text_only_and_validates_bounds_timing_and_attribution(http_harness, changes):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    before = h.snapshot()
    assert ingest(h, client, "dm", **changes).status_code == 422
    assert h.snapshot() == before


def test_ingest_ended_or_foreign_session_is_rejected_without_mutation(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    for session_id in (str(uuid4()), h.rows["other_session"].id):
        before = h.snapshot()
        assert ingest(h, client, session_id=session_id).status_code == 404
        assert h.snapshot() == before
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    assert ingest(h, client).status_code == 409
    assert h.snapshot() == before


def test_ingest_lock_order_refreshes_session_then_share_locks_consent(http_harness, monkeypatch):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    execute = h.session.exec
    statements = []

    def record(statement):
        if getattr(statement, "_for_update_arg", None) is not None:
            statements.append(statement)
        return execute(statement)

    monkeypatch.setattr(h.session, "exec", record)
    assert ingest(h, client).status_code == 200
    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
    assert "game_session" in sql[0] and sql[0].endswith("FOR UPDATE")
    assert "transcript_consent" in sql[1] and sql[1].endswith("FOR SHARE")
    assert "game_session" in sql[2] and sql[2].endswith("FOR UPDATE")
    assert all(statement.get_execution_options()["populate_existing"] for statement in statements)


def test_ingest_refreshes_stale_session_before_admitting_text(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    cached = h.rows["campaign_session"]
    assert cached.transcript_state == "on"
    with Session(h.session.get_bind()) as other:
        row = other.get(GameSession, cached.id)
        row.transcript_state = "paused"
        other.commit()
    assert cached.transcript_state == "on"
    assert ingest(h, client).status_code == 409
    h.session.refresh(cached)
    assert cached.transcript_state == "paused"


def test_dm_pc_aside_uses_existing_audience_and_hides_other_member_ids(http_harness):
    h, client = http_harness
    assert state(h, client, "on").status_code == 200
    response = ingest(h, client, "dm", source="table", speaker_member_id=h.rows["member_dm"].id,
                      audience={"kind": "pcs", "pc_ids": [h.rows["character_a"].id]})
    assert response.status_code == 200, response.text
    feed = PREFIX.format(campaign_id=h.rows["campaign"].id) + f"/sessions/{h.rows['campaign_session'].id}/events"
    for viewer in ("dm", "player_a", "player_b"):
        result = client.get(feed, headers=h.headers(viewer))
        assert result.status_code == 200
        h.assert_no_leak(result, viewer)
        matches = [event for event in result.json() if event["id"] == response.json()["id"]]
        assert bool(matches) is (viewer in ("dm", "player_a"))
        if viewer == "player_a":
            assert matches[0]["author_member_id"] is None


def own_rows(h, viewer="player_a"):
    member = h.rows["member" if viewer == "player_b" else f"member_{viewer}"]
    return h.session.exec(
        select(TranscriptConsent).where(TranscriptConsent.member_id == member.id)
    ).all()


def events(h):
    return h.session.exec(
        select(SessionEvent)
        .where(
            SessionEvent.session_id == h.rows["campaign_session"].id,
            SessionEvent.kind == "system",
        )
        .order_by(SessionEvent.seq)
    ).all()


def test_consent_grant_and_same_processor_retry_are_idempotent(http_harness):
    h, client = http_harness
    assert client.delete(url(h), headers=h.headers("player_a")).status_code == 204
    response = grant(h, client)
    assert response.status_code == 200, response.text
    assert response.json()["processor"] == "Local STT"
    before = h.snapshot()
    assert grant(h, client).json() == response.json()
    assert h.snapshot() == before
    assert len([r for r in own_rows(h) if r.revoked_at is None]) == 1


def test_consent_member_lock_serializes_writers_without_blocking_author_fk(
    http_harness, monkeypatch
):
    h, _ = http_harness
    execute = h.session.exec
    statements = []

    def record(statement):
        statements.append(statement)
        return execute(statement)

    monkeypatch.setattr(h.session, "exec", record)
    _lock_consent_member(h.session, h.rows["member_player_a"])
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert sql.endswith("FOR NO KEY UPDATE")
    assert statements[0].get_execution_options()["populate_existing"] is True


def test_processor_change_revokes_old_row_and_preserves_history(http_harness):
    h, client = http_harness
    assert grant(h, client, processor="First").status_code == 200
    first = next(r for r in own_rows(h) if r.revoked_at is None)
    original_grant = first.granted_at
    assert grant(h, client, processor="Second").status_code == 200
    h.session.refresh(first)
    assert first.granted_at == original_grant
    assert first.revoked_at is not None
    active = [r for r in own_rows(h) if r.revoked_at is None]
    assert len(active) == 1
    assert active[0].processor == "Second"
    assert active[0].id != first.id


def test_revoke_is_an_idempotent_update_and_regrant_creates_new_row(http_harness):
    h, client = http_harness
    assert grant(h, client).status_code == 200
    active = next(r for r in own_rows(h) if r.revoked_at is None)
    ids_before = {r.id for r in own_rows(h)}
    assert client.delete(url(h), headers=h.headers("player_a")).status_code == 204
    h.session.refresh(active)
    assert active.revoked_at is not None
    assert {r.id for r in own_rows(h)} == ids_before
    before = h.snapshot()
    assert client.delete(url(h), headers=h.headers("player_a")).status_code == 204
    assert h.snapshot() == before
    assert grant(h, client).status_code == 200
    assert next(r for r in own_rows(h) if r.revoked_at is None).id != active.id


@pytest.mark.parametrize("viewer", ("dm", "player_a", "player_b", "no_character"))
def test_every_member_can_manage_only_own_consent(http_harness, viewer):
    h, client = http_harness
    assert grant(h, client, viewer).status_code == 200
    assert client.delete(url(h), headers=h.headers(viewer)).status_code == 204
    assert all(r.revoked_at is not None for r in own_rows(h, viewer))
    before = h.snapshot()
    response = client.put(
        url(h),
        headers=h.headers(viewer),
        json={
            "processor": "Adapter",
            "member_id": h.rows["member"].id,
        },
    )
    assert response.status_code == 422
    assert h.snapshot() == before


def test_player_consent_view_has_no_member_ids_and_dm_reads_all_rows(http_harness):
    h, client = http_harness
    for viewer in ("player_a", "player_b", "no_character"):
        response = client.get(url(h), headers=h.headers(viewer))
        assert response.status_code == 200
        h.assert_no_leak(response, viewer)
        assert response.json()["consents"]
        assert all(
            set(row) == {"processor", "granted_at", "revoked_at"}
            for row in response.json()["consents"]
        )
        assert "member_id" not in response.text
    response = client.get(url(h), headers=h.headers("dm"))
    assert response.status_code == 200
    h.assert_no_leak(response, "dm")
    expected = h.session.exec(
        select(TranscriptConsent).where(
            TranscriptConsent.campaign_id == h.rows["campaign"].id
        )
    ).all()
    assert {r["member_id"] for r in response.json()["consents"]} == {
        r.member_id for r in expected
    }
    assert len(response.json()["consents"]) == len(expected)


@pytest.mark.parametrize("processor", ("", "x" * 121))
def test_invalid_processor_is_rejected_without_mutation(http_harness, processor):
    h, client = http_harness
    before = h.snapshot()
    assert grant(h, client, processor=processor).status_code == 422
    assert h.snapshot() == before


def test_dm_state_transitions_append_one_table_event_each(http_harness):
    h, client = http_harness
    previous = "off"
    before_count = len(events(h))
    for index, value in enumerate(("on", "paused", "off"), start=1):
        response = state(h, client, value)
        assert response.status_code == 200, response.text
        assert response.json() == {"state": value}
        assert len(events(h)) == before_count + index
        row = events(h)[-1]
        assert row.body == {"transcript_state": value, "previous": previous}
        assert row.audience == "table"
        assert row.author_member_id == h.rows["member_dm"].id
        before = h.snapshot()
        assert state(h, client, value).status_code == 200
        assert h.snapshot() == before
        for viewer in ("dm", "player_a", "player_b", "no_character"):
            assert client.get(url(h, state=True), headers=h.headers(viewer)).json() == {
                "state": value
            }
        previous = value


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
def test_player_pause_only_from_on_and_retry_writes_exactly_one_event(
    http_harness, viewer
):
    h, client = http_harness
    before = h.snapshot()
    assert state(h, client, "paused", viewer).status_code == 409
    assert h.snapshot() == before
    assert state(h, client, "on").status_code == 200
    before_count = len(events(h))
    response = state(h, client, "paused", viewer)
    assert response.status_code == 200, response.text
    assert len(events(h)) == before_count + 1
    row = events(h)[-1]
    assert row.body == {"transcript_state": "paused", "previous": "on"}
    assert row.audience == "table"
    assert (
        row.author_member_id
        == h.rows["member" if viewer == "player_b" else f"member_{viewer}"].id
    )
    before = h.snapshot()
    assert state(h, client, "paused", viewer).status_code == 200
    assert h.snapshot() == before


@pytest.mark.parametrize("value", ("on", "off"))
@pytest.mark.parametrize("previous", ("on", "off", "paused"))
def test_player_cannot_set_on_or_off(http_harness, value, previous):
    h, client = http_harness
    assert state(h, client, previous).status_code == 200
    before = h.snapshot()
    assert state(h, client, value, "player_a").status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("dm", "player_a"))
def test_ended_session_refuses_even_noop_state_change(http_harness, viewer):
    h, client = http_harness
    assert state(h, client, "paused").status_code == 200
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    assert state(h, client, "paused", viewer).status_code == 409
    assert h.snapshot() == before


def test_state_write_refreshes_stale_session_before_checking_end(http_harness):
    h, client = http_harness
    cached = h.rows["campaign_session"]
    assert cached.status == "active"
    with Session(h.session.get_bind()) as other:
        persisted = other.get(GameSession, cached.id)
        persisted.status = "ended"
        other.commit()
    assert cached.status == "active"
    count = len(events(h))
    assert state(h, client, "on").status_code == 409
    h.session.refresh(cached)
    assert cached.status == "ended"
    assert cached.transcript_state == "off"
    assert len(events(h)) == count


def test_transcript_state_scopes_session_to_campaign(http_harness):
    h, client = http_harness
    before = h.snapshot()
    for method in ("get", "put"):
        kwargs = {"json": {"state": "on"}} if method == "put" else {}
        for session_id in (str(uuid4()), h.rows["other_session"].id):
            response = getattr(client, method)(
                url(h, state=True, game_session=session_id),
                headers=h.headers("dm"),
                **kwargs,
            )
            assert response.status_code == 404, response.text
            assert h.snapshot() == before


def transcript_routes(app):
    routes = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and any(
            d.call is require_transcript_enabled for d in route.dependant.dependencies
        ):
            routes.update((method, context.path) for method in context.methods)
    return routes


@pytest.mark.parametrize(
    "play,transcript", (("false", "false"), ("true", "false"), ("false", "true"))
)
def test_every_derived_transcript_route_is_hidden_when_either_flag_is_off(
    http_harness, monkeypatch, play, transcript
):
    h, client = http_harness
    derived = transcript_routes(client.app)
    assert derived == EXPECTED_ROUTES
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", play)
    monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", transcript)
    before = h.snapshot()
    for method, path in derived:
        body = {"state": "on"} if path == STATE else {"processor": "Local"}
        if method == "POST":
            body = {
                "text": "Hidden utterance", "source": "browser", "confidence": 1,
                "started_at": "2026-10-10T12:00:00Z",
                "ended_at": "2026-10-10T12:00:01Z",
            }
        kwargs = {"json": body} if method in ("PUT", "POST") else {}
        response = client.request(
            method,
            path.format(
                campaign_id=h.rows["campaign"].id,
                session_id=h.rows["campaign_session"].id,
            ),
            headers=h.headers("dm"),
            **kwargs,
        )
        assert response.status_code == 404, (method, path, response.text)
        assert h.snapshot() == before


@pytest.mark.parametrize(
    "value", (None, "TRUE", "True", "1", " true", "true ", "false", "true")
)
def test_transcript_flag_is_exact_and_read_at_call_time(monkeypatch, value):
    monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", "true")
    assert transcript_enabled()
    if value is None:
        monkeypatch.delenv("GRIMOIRE_TRANSCRIPT_ENABLED")
    else:
        monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", value)
    assert transcript_enabled() is (value == "true")


@pytest.mark.parametrize(
    "mutation",
    (
        "processor_empty",
        "processor_long",
        "duplicate",
        "revocation_before_grant",
        "state",
        "retention_low",
        "retention_high",
    ),
)
def test_transcript_schema_constraints(http_harness, mutation):
    h, _ = http_harness
    active = next(r for r in own_rows(h) if r.revoked_at is None)
    if mutation.startswith("processor"):
        active.processor = "" if mutation == "processor_empty" else "x" * 121
    elif mutation == "duplicate":
        h.session.add(
            TranscriptConsent(
                campaign_id=active.campaign_id,
                member_id=active.member_id,
                processor="Duplicate",
            )
        )
    elif mutation == "revocation_before_grant":
        active.revoked_at = active.granted_at - timedelta(seconds=1)
    elif mutation == "state":
        h.rows["campaign_session"].transcript_state = "invalid"
    else:
        h.rows["campaign"].transcript_retention_days = (
            0 if mutation == "retention_low" else 366
        )
    with pytest.raises(IntegrityError):
        h.session.flush()
    h.session.rollback()


def test_schema_defaults_are_off_and_thirty_days():
    assert GameSession(campaign_id=str(uuid4())).transcript_state == "off"
    assert Campaign(name="Default").transcript_retention_days == 30


@pytest.mark.parametrize("state_route", (False, True))
def test_control_requests_never_accept_audio_or_unknown_fields(
    http_harness, state_route
):
    h, client = http_harness
    before = h.snapshot()
    body = {"state": "on"} if state_route else {"processor": "Local"}
    for field in ("audio", "text", "unknown"):
        response = client.put(
            url(h, state=state_route),
            headers=h.headers("dm"),
            json={**body, field: "payload"},
        )
        assert response.status_code == 422
        assert h.snapshot() == before
