"""Tests for moving milestone Google Calendar synchronization."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import Session

from moving import gcal_sync
from moving.models import GcalTombstone, Milestone

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_TOKEN_URL = "https://oauth2.googleapis.com/token"


@pytest.fixture(autouse=True)
def calendar_credential(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")


def _response(request: httpx.Request, status: int, body: dict | None = None):
    return (
        httpx.Response(status, request=request, json=body)
        if body
        else httpx.Response(status, request=request)
    )


def _install_transport(monkeypatch, responder):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == _TOKEN_URL:
            return _response(request, 200, {"access_token": "access"})
        return responder(request)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(gcal_sync.httpx, "AsyncClient", client_factory)
    return requests


def _event_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if str(request.url) != _TOKEN_URL]


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _add_milestone(session: Session, **values) -> Milestone:
    milestone = Milestone(
        title=values.pop("title", "Collect keys"),
        occurs_on=values.pop("occurs_on", date(2026, 10, 7)),
        **values,
    )
    session.add(milestone)
    session.commit()
    return milestone


def test_unconfigured_handler_skips_network_and_database(monkeypatch):
    for key in (
        "GOOGLE_CALENDAR_CLIENT_ID",
        "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_CALENDAR_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("unconfigured handler must not access network or database")

    monkeypatch.setattr(gcal_sync.httpx, "AsyncClient", unexpected)
    monkeypatch.setattr(gcal_sync, "_load_milestones", unexpected)
    monkeypatch.setattr(gcal_sync, "_load_tombstones", unexpected)

    assert asyncio.run(gcal_sync.gcal_sync_handler(object())) is None


def test_queued_insert_uses_exclusive_all_day_end(session: Session, monkeypatch):
    milestone = _add_milestone(session)

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return _response(request, 200, {"id": "event-new"})

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1

    event_request = _event_requests(requests)[0]
    assert _json(event_request) == {
        "summary": "Collect keys",
        "start": {"date": "2026-10-07"},
        "end": {"date": "2026-10-08"},
    }
    session.expire_all()
    synced = session.get(Milestone, milestone.id)
    assert synced.gcal_event_id == "event-new"
    assert synced.gcal_state == "synced"
    assert isinstance(synced.gcal_synced_at, datetime)


def test_synced_no_drift_does_not_patch(session: Session, monkeypatch):
    synced_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    milestone = _add_milestone(
        session,
        gcal_state="synced",
        gcal_event_id="event-existing",
        gcal_synced_at=synced_at,
    )

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return _response(
            request,
            200,
            {
                "summary": "Collect keys",
                "start": {"date": "2026-10-07"},
                "end": {"date": "2026-10-08"},
            },
        )

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 0
    assert [request.method for request in _event_requests(requests)] == ["GET"]
    session.expire_all()
    assert (
        session.get(Milestone, milestone.id).gcal_synced_at.replace(tzinfo=timezone.utc)
        == synced_at
    )


def test_synced_drift_patches_and_refreshes_timestamp(session: Session, monkeypatch):
    old_synced_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    milestone = _add_milestone(
        session,
        gcal_state="synced",
        gcal_event_id="event-existing",
        gcal_synced_at=old_synced_at,
    )

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(
                request,
                200,
                {"summary": "Old title", "start": {"date": "2026-10-06"}},
            )
        assert request.method == "PATCH"
        return _response(request, 200, {})

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1
    event_requests = _event_requests(requests)
    assert [request.method for request in event_requests] == ["GET", "PATCH"]
    assert _json(event_requests[1]) == {
        "summary": "Collect keys",
        "start": {"date": "2026-10-07"},
        "end": {"date": "2026-10-08"},
    }
    session.expire_all()
    refreshed = session.get(Milestone, milestone.id).gcal_synced_at
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    assert refreshed > old_synced_at


@pytest.mark.parametrize("gone_status", [404, 410])
def test_synced_gone_event_is_reinserted(
    session: Session, monkeypatch, gone_status: int
):
    milestone = _add_milestone(session, gcal_state="synced", gcal_event_id="event-gone")

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(request, gone_status, {"error": "gone"})
        assert request.method == "POST"
        return _response(request, 200, {"id": "event-replacement"})

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1
    assert [request.method for request in _event_requests(requests)] == ["GET", "POST"]
    session.expire_all()
    assert session.get(Milestone, milestone.id).gcal_event_id == "event-replacement"


def test_held_milestone_is_untouched(session: Session, monkeypatch):
    milestone = _add_milestone(session, gcal_state="held", gcal_event_id="event-held")

    def responder(request: httpx.Request) -> httpx.Response:
        raise AssertionError("held milestone must not make a calendar request")

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 0
    assert requests == []
    session.expire_all()
    assert session.get(Milestone, milestone.id).gcal_synced_at is None


@pytest.mark.parametrize("status", [204, 404, 410])
def test_tombstone_success_and_gone_responses_drain_row(
    session: Session, monkeypatch, status: int
):
    session.add(GcalTombstone(event_id="event-delete"))
    session.commit()

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return _response(request, status, {"error": "gone"} if status != 204 else None)

    _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1
    assert session.get(GcalTombstone, "event-delete") is None


def test_tombstone_failure_keeps_row(session: Session, monkeypatch):
    session.add(GcalTombstone(event_id="event-retry"))
    session.commit()

    def responder(request: httpx.Request) -> httpx.Response:
        return _response(request, 500, {"error": "retry"})

    _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 0
    assert session.get(GcalTombstone, "event-retry") is not None


def test_per_row_error_does_not_stop_next_milestone(session: Session, monkeypatch):
    failed = _add_milestone(session, title="Fail")
    succeeded = _add_milestone(session, title="Succeed", occurs_on=date(2026, 10, 8))

    def responder(request: httpx.Request) -> httpx.Response:
        body = _json(request)
        if body["summary"] == "Fail":
            return _response(request, 500, {"error": "retry"})
        return _response(request, 200, {"id": "event-success"})

    _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1
    session.expire_all()
    assert session.get(Milestone, failed.id).gcal_state == "queued"
    assert session.get(Milestone, succeeded.id).gcal_event_id == "event-success"


@pytest.mark.parametrize(
    ("moving_id", "google_id", "expected"),
    [
        ("moving@example.test", "household@example.test", "moving@example.test"),
        ("", "household@example.test", "household@example.test"),
        ("", None, "primary"),
    ],
)
def test_calendar_id_fallback(
    session: Session,
    monkeypatch,
    moving_id: str,
    google_id: str | None,
    expected: str,
):
    _add_milestone(session)
    monkeypatch.setenv("MOVING_GCAL_CALENDAR_ID", moving_id)
    if google_id is None:
        monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", google_id)

    def responder(request: httpx.Request) -> httpx.Response:
        assert f"/calendars/{expected}/events" in str(request.url)
        return _response(request, 200, {"id": "event-id"})

    _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1


def test_requeued_milestone_with_event_id_heals_instead_of_inserting(
    session: Session, monkeypatch
):
    milestone = _add_milestone(session, gcal_state="queued", gcal_event_id="event-kept")

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "re-queued row must never POST a duplicate"
        return _response(
            request,
            200,
            {
                "summary": "Collect keys",
                "start": {"date": "2026-10-07"},
                "end": {"date": "2026-10-08"},
            },
        )

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 0
    assert [request.method for request in _event_requests(requests)] == ["GET"]
    session.expire_all()
    assert session.get(Milestone, milestone.id).gcal_event_id == "event-kept"


def test_end_date_drift_alone_triggers_patch(session: Session, monkeypatch):
    _add_milestone(
        session,
        gcal_state="synced",
        gcal_event_id="event-stretched",
        gcal_synced_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(
                request,
                200,
                {
                    "summary": "Collect keys",
                    "start": {"date": "2026-10-07"},
                    "end": {"date": "2026-10-09"},
                },
            )
        assert request.method == "PATCH"
        return _response(request, 200, {})

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 1
    assert [request.method for request in _event_requests(requests)] == [
        "GET",
        "PATCH",
    ]


def test_hold_taken_mid_run_survives_apply(session: Session):
    milestone = _add_milestone(session, gcal_state="held")
    outcome = gcal_sync._SyncOutcome(
        milestone_updates=[
            gcal_sync._MilestoneUpdate(
                id=milestone.id,
                event_id="event-late",
                synced_at=datetime.now(timezone.utc),
            )
        ]
    )

    gcal_sync._apply_sync(session, outcome)
    session.commit()
    session.expire_all()
    held = session.get(Milestone, milestone.id)
    assert held.gcal_state == "held"
    assert held.gcal_event_id == "event-late"
    assert held.gcal_synced_at is None


def test_milestone_deleted_mid_run_tombstones_its_fresh_event(session: Session):
    outcome = gcal_sync._SyncOutcome(
        milestone_updates=[
            gcal_sync._MilestoneUpdate(
                id="00000000-0000-0000-0000-000000000000",
                event_id="event-orphan",
                synced_at=datetime.now(timezone.utc),
            )
        ]
    )

    assert gcal_sync._apply_sync(session, outcome) == 1
    session.commit()
    assert session.get(GcalTombstone, "event-orphan") is not None


def test_expired_tombstone_is_dropped_without_a_delete_call(
    session: Session, monkeypatch
):
    session.add(
        GcalTombstone(
            event_id="event-undeletable",
            created_at=datetime.now(timezone.utc)
            - timedelta(days=gcal_sync.TOMBSTONE_RETRY_DAYS + 1),
        )
    )
    session.commit()

    def responder(request: httpx.Request) -> httpx.Response:
        raise AssertionError("expired tombstone must not reach the API")

    requests = _install_transport(monkeypatch, responder)
    assert gcal_sync.sync_milestones(session) == 0
    assert requests == []
    assert session.get(GcalTombstone, "event-undeletable") is None


def test_configured_handler_syncs_through_fresh_sessions(session: Session, monkeypatch):
    milestone = _add_milestone(session)

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return _response(request, 200, {"id": "event-handler"})

    _install_transport(monkeypatch, responder)
    monkeypatch.setattr("core.db.get_engine", lambda: session.get_bind())

    assert asyncio.run(gcal_sync.gcal_sync_handler(object())) is None
    session.expire_all()
    synced = session.get(Milestone, milestone.id)
    assert synced.gcal_event_id == "event-handler"
    assert synced.gcal_state == "synced"
