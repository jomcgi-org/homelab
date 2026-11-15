"""Offline coverage of UTC grounding delivered by both real agent constructors."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, SystemPromptPart, TextPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import (
    MODEL_KNOWLEDGE_CUTOFF,
    ChatDeps,
    create_agent,
    create_fact_check_agent,
    temporal_grounding_prompt,
    today_str,
)


UTC_CASES = [
    (datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc), "5 November 2026"),
    (datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone.utc), "5 November 2026"),
    (datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc), "6 November 2026"),
    (datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc), "1 January 2027"),
]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Any accidental provider, HTTP, tool or embedding request must fail locally."""
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    with (
        patch("httpx.Client.send", side_effect=AssertionError("outbound HTTP")) as sync,
        patch(
            "httpx.AsyncClient.send", side_effect=AssertionError("outbound HTTP")
        ) as async_send,
        patch(
            "chat.agent.search_web", side_effect=AssertionError("web tool request")
        ) as search,
        patch(
            "chat.agent.EmbeddingClient.embed",
            side_effect=AssertionError("embedding request"),
        ) as embed,
        patch(
            "chat.agent.EmbeddingClient.embed_batch",
            side_effect=AssertionError("embedding request"),
        ) as embed_batch,
    ):
        yield
    for request in (sync, async_send, search, embed, embed_batch):
        request.assert_not_called()


@pytest.mark.parametrize("instant, expected_date", UTC_CASES)
def test_utc_date_and_grounding(instant, expected_date):
    with patch("chat.agent.datetime") as clock:
        clock.now.return_value = instant
        assert today_str() == expected_date
        clock.now.assert_called_once_with(timezone.utc)
        clock.now.reset_mock()
        prompt = temporal_grounding_prompt()
        assert expected_date in prompt
        assert MODEL_KNOWLEDGE_CUTOFF in prompt
        clock.now.assert_called_once_with(timezone.utc)


@pytest.mark.parametrize(
    "constructor", [create_agent, create_fact_check_agent], ids=["chat", "fact_check"]
)
@pytest.mark.asyncio
async def test_registered_grounding_is_evaluated_per_fresh_run(constructor):
    captured = []

    def terminal_model(messages, info):
        captured.append(
            [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, SystemPromptPart)
            ]
        )
        return ModelResponse(parts=[TextPart("done")])

    store = MagicMock(spec=[])
    embed_client = AsyncMock(spec=[])
    deps = ChatDeps(
        channel_id="offline-channel",
        author_id="offline-author",
        store=store,
        embed_client=embed_client,
    )
    with (
        patch("chat.agent.datetime") as clock,
        patch("shared.inference.hosted_chat_provider", return_value=None),
        patch("shared.inference.auth_headers", return_value={}),
        patch("chat.directives.get_active", return_value=""),
        patch("chat.directives.get_style_pref", return_value=""),
    ):
        clock.now.return_value = UTC_CASES[0][0]
        agent = constructor(base_url="http://offline.invalid:8080")
        model = FunctionModel(terminal_model)
        fragments = []
        for index, (instant, expected_date) in enumerate(UTC_CASES[:3]):
            clock.now.return_value = instant
            clock.now.reset_mock()
            result = await agent.run("go", model=model, deps=deps)
            assert result.output == "done"
            assert result.usage().tool_calls == 0
            clock.now.assert_called_once_with(timezone.utc)
            assert len(captured) == index + 1
            temporal = [
                part for part in captured[index] if MODEL_KNOWLEDGE_CUTOFF in part
            ]
            assert len(temporal) == 1
            assert expected_date in temporal[0]
            fragments.append(temporal[0].encode("utf-8"))

    assert fragments[0] == fragments[1]
    assert fragments[1] != fragments[2]
    assert store.mock_calls == []
    assert embed_client.mock_calls == []
