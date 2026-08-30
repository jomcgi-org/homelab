"""Tests for ephemeral moving-plan chat."""

import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.db import get_session
from moving import chat
from moving.models import Task, Viewer

_EMAIL = "joe@example.test"
_HEADERS = {"X-Auth-Email": _EMAIL}


@pytest.fixture(name="client")
def client_fixture(session: Session, monkeypatch: pytest.MonkeyPatch):
    session.add(Viewer(email=_EMAIL, name="joe"))
    session.commit()
    monkeypatch.setattr(chat, "INFERENCE_URL", "http://inference.test")

    app = FastAPI()
    app.include_router(chat.router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "body",
    [
        {"message": "x" * (chat.MESSAGE_CHAR_CAP + 1)},
        {
            "message": "hello",
            "history": [
                {
                    "role": "user",
                    "content": "x" * (chat.HISTORY_MESSAGE_CAP + 1),
                }
            ],
        },
    ],
)
def test_message_and_history_caps_return_422(client: TestClient, body: dict):
    response = client.post("/api/moving/chat", headers=_HEADERS, json=body)
    assert response.status_code == 422


def test_unknown_viewer_returns_403(client: TestClient):
    response = client.post(
        "/api/moving/chat",
        headers={"X-Auth-Email": "unknown@example.test"},
        json={"message": "What is next?"},
    )
    assert response.status_code == 403


def test_build_model_messages_frames_state_and_bounds_history(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MOVING_CHAT_SYSTEM_PROMPT", "Custom moving helper")
    request = chat.ChatRequest(
        message="What is next?",
        history=[
            chat.HistoryMessage(role="user", content=f"turn {index}")
            for index in range(chat.HISTORY_LIMIT + 3)
        ],
    )
    state = {
        "tasks": [{"title": "Pack books", "due_on": date(2026, 9, 1)}],
        "milestones": [],
        "spans": [],
        "roles": [],
        "collisions": [],
        "progress": 0.0,
        "viewer": "joe",
    }

    messages = chat.build_model_messages(request, state, "joe", today=date(2026, 8, 29))

    assert "Custom moving helper" in messages[0]["content"]
    assert "2026-08-29" in messages[0]["content"]
    assert "current viewer is joe" in messages[0]["content"]
    assert "<move_plan>\n" in messages[1]["content"]
    assert "\n</move_plan>" in messages[1]["content"]
    assert '"title": "Pack books"' in messages[1]["content"]
    assert '"due_on": "2026-09-01"' in messages[1]["content"]
    history = messages[2:-1]
    assert len(history) == chat.HISTORY_LIMIT
    assert history[0]["content"] == "turn 3"
    assert messages[-1] == {"role": "user", "content": "What is next?"}


def test_stream_endpoint_emits_sse_frames(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    session.add(Task(title="Pack boxes", owner="both"))
    session.commit()
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"Pack "}}]}'
            yield 'data: {"choices":[{"delta":{"content":"boxes."}}]}'
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, *, json):
            captured.update(method=method, url=url, body=json)
            return StreamContext()

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)

    response = client.post(
        "/api/moving/chat",
        headers=_HEADERS,
        json={"message": "What should I do?", "history": []},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames == [
        {"type": "token", "data": {"text": "Pack "}},
        {"type": "token", "data": {"text": "boxes."}},
        {"type": "done", "data": {}},
    ]
    assert captured["method"] == "POST"
    assert captured["url"] == "http://inference.test/v1/chat/completions"
    assert captured["body"]["model"] == chat.MODEL
    assert captured["body"]["max_tokens"] == chat.MAX_TOKENS
    assert "tools" not in captured["body"]
    assert "Pack boxes" in captured["body"]["messages"][1]["content"]
