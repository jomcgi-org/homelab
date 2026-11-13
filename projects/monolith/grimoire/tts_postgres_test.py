"""Both speech routes query native UUID columns in migrated PostgreSQL."""

import base64
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.db import get_session
from grimoire import tts
from grimoire.access import get_authenticated_email
from grimoire.models import AppUser, Campaign, CampaignMember, GameSession, SessionEvent
from grimoire.router import router

AUDIO = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 36


@pytest.mark.parametrize("uppercase", [False, True])
def test_speech_routes_select_native_uuid(session, monkeypatch, uppercase):
    assert session.bind.dialect.name == "postgresql"
    monkeypatch.setenv("GRIMOIRE_TTS_ENABLED", "true")
    monkeypatch.setattr(tts, "object_cache", lambda: None)
    monkeypatch.setattr(tts, "service", tts.SpeechService())
    requests = []

    def synthesize(request):
        requests.append(request)
        return httpx.Response(200, content=AUDIO)

    transport = httpx.MockTransport(synthesize)
    real_client = httpx.Client
    monkeypatch.setattr(
        tts.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    campaign = Campaign(name="Speech regression")
    user = AppUser(email=f"tts-{uuid4()}@example.test")
    session.add_all([campaign, user])
    session.flush()
    member = CampaignMember(campaign_id=campaign.id, app_user_id=user.id, role="dm")
    game_session = GameSession(campaign_id=campaign.id)
    session.add_all([member, game_session])
    session.flush()
    event = SessionEvent(
        campaign_id=campaign.id,
        session_id=game_session.id,
        seq=1,
        kind="narration",
        audience="table",
        author_member_id=member.id,
        body={"text": "The captain speaks."},
    )
    session.add(event)
    session.flush()
    event_id = event.id.upper() if uppercase else event.id
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_authenticated_email] = lambda: user.email
    path = f"/api/grimoire/campaigns/{campaign.id}/tts"
    with TestClient(app) as client:
        created = client.post(path, json={"event_id": event_id})
        assert created.status_code == 200, created.text
        clip_id = created.json()["clip_id"]
        retrieved = client.get(f"{path}/{event_id}/{clip_id}")
        assert retrieved.status_code == 200, retrieved.text
        for response in (created, retrieved):
            assert response.headers["cache-control"] == "private, no-store"
            assert base64.b64decode(response.json()["audio_base64"]) == AUDIO
    assert len(requests) == 1
