from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import dataclasses
import home.module
from framework import PRIVATE_PROFILE, build_app

# Compose only the home domain instead of the whole monolith: the
# same framework wiring the production app gets, without depending on
# the app composition root, which imports every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (home.module.MODULE,),
)

MOCK_EVENTS = [
    {"time": None, "title": "Holiday", "allDay": True},
    {"time": "09:00", "endTime": "09:30", "title": "Standup", "allDay": False},
]


@pytest.fixture(name="client")
def client_fixture():
    return TestClient(app, raise_server_exceptions=False)


def test_schedule_today_returns_events(client):
    with patch("home.schedule_router.get_today_events", return_value=MOCK_EVENTS):
        response = client.get("/api/home/schedule/today")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["allDay"] is True
    assert data[1]["time"] == "09:00"


def test_schedule_today_empty(client):
    with patch("home.schedule_router.get_today_events", return_value=[]):
        response = client.get("/api/home/schedule/today")
    assert response.status_code == 200
    assert response.json() == []
