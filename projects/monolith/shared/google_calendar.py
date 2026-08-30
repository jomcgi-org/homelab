"""Reusable Google Calendar OAuth and event operations."""

from __future__ import annotations

import os
from datetime import date, timedelta

import httpx


def configured() -> bool:
    """Return whether the complete Google Calendar OAuth credential is present."""
    return all(
        os.environ.get(key)
        for key in (
            "GOOGLE_CALENDAR_CLIENT_ID",
            "GOOGLE_CALENDAR_CLIENT_SECRET",
            "GOOGLE_CALENDAR_REFRESH_TOKEN",
        )
    )


def _token_url() -> str:
    return "https://oauth2.googleapis.com/token"


def _events_url() -> str:
    return "https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"


def _calendar_events_url(cal_id: str) -> str:
    return _events_url().format(cal_id=cal_id)


async def _access_token(client: httpx.AsyncClient) -> str:
    """Exchange the configured refresh token for a short-lived access token."""
    response = await client.post(
        _token_url(),
        data={
            "client_id": os.environ["GOOGLE_CALENDAR_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CALENDAR_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_CALENDAR_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def insert_event_json(client: httpx.AsyncClient, cal_id: str, body: dict) -> dict:
    """Insert an event body and return Google's created-event response."""
    token = await _access_token(client)
    response = await client.post(
        _calendar_events_url(cal_id),
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    response.raise_for_status()
    return response.json()


def _all_day_body(title: str, starts_on: date, ends_on: date | None = None) -> dict:
    exclusive_end = ends_on or (starts_on + timedelta(days=1))
    return {
        "summary": title,
        "start": {"date": starts_on.isoformat()},
        "end": {"date": exclusive_end.isoformat()},
    }


async def insert_event(
    client: httpx.AsyncClient,
    cal_id: str,
    title: str,
    starts_on: date,
    ends_on: date | None = None,
) -> str:
    """Insert an all-day event and return its Google event id."""
    created = await insert_event_json(
        client, cal_id, _all_day_body(title, starts_on, ends_on)
    )
    return created["id"]


async def get_event(client: httpx.AsyncClient, cal_id: str, event_id: str) -> dict:
    """Fetch an event by id, raising on transport or API errors."""
    token = await _access_token(client)
    response = await client.get(
        f"{_calendar_events_url(cal_id)}/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


async def patch_event(
    client: httpx.AsyncClient,
    cal_id: str,
    event_id: str,
    summary: str,
    starts_on: date,
    ends_on: date | None = None,
) -> None:
    """Update an all-day event, raising on transport or API errors."""
    token = await _access_token(client)
    response = await client.patch(
        f"{_calendar_events_url(cal_id)}/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
        json=_all_day_body(summary, starts_on, ends_on),
    )
    response.raise_for_status()


async def delete_event(client: httpx.AsyncClient, cal_id: str, event_id: str) -> None:
    """Delete an event, treating already-absent events as success."""
    token = await _access_token(client)
    response = await client.delete(
        f"{_calendar_events_url(cal_id)}/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code not in (404, 410):
        response.raise_for_status()
