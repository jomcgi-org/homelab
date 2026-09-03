"""Additional coverage for summarizer -- build_llm_caller() and error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import Message, UserChannelSummary
from chat.summarizer import build_llm_caller, generate_summaries
import shared.inference


# ---------------------------------------------------------------------------
# Session fixture (in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session (schema-stripped for SQLite compat)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _make_message(session, channel_id, user_id, username, content, msg_id):
    msg = Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        content=content,
        is_bot=False,
        embedding=[0.0] * 1024,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


# ---------------------------------------------------------------------------
# TestBuildLlmCaller -- factory function
# ---------------------------------------------------------------------------


class TestBuildLlmCaller:
    @pytest.mark.asyncio
    async def test_sends_correct_payload(self):
        """build_llm_caller() sends model, messages, and max_tokens in the request."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Summary text"}}]
        }
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            result = await caller("Summarize this conversation.")

        assert result == "Summary text"
        mock_instance.post.assert_called_once()
        call_kwargs = mock_instance.post.call_args
        url_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("url")
        assert url_arg == "http://fake:8080/v1/chat/completions"
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == shared.inference.META_SPARK_MODEL
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Summarize this conversation."
        # max_tokens must leave room for the prompt within the 32768 context;
        # reserving the whole window 400s on any non-empty prompt.
        assert payload["max_tokens"] == 8192
        assert "chat_template_kwargs" not in payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("api_key", "expected_headers"),
        [
            ("backend-spark-key", {"Authorization": "Bearer backend-spark-key"}),
            ("", {}),
        ],
    )
    async def test_uses_optional_meta_spark_bearer(
        self, monkeypatch, api_key, expected_headers
    ):
        monkeypatch.setenv("META_SPARK_API_KEY", api_key)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "choices": [{"message": {"content": "Summary text"}}]
        }
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=response)

        with patch("chat.summarizer.httpx.AsyncClient", return_value=http_client):
            caller = build_llm_caller("https://api.meta.ai")
            await caller("Summarize this conversation.")

        assert http_client.post.call_args.kwargs["headers"] == expected_headers

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_missing_choices(self):
        """build_llm_caller() raises RuntimeError when 'choices' key is absent."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"unexpected": "shape"}
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            with pytest.raises(RuntimeError, match="unexpected LLM response shape"):
                await caller("some prompt")

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_empty_choices(self):
        """build_llm_caller() raises RuntimeError when 'choices' list is empty."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": []}
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            with pytest.raises(RuntimeError, match="unexpected LLM response shape"):
                await caller("some prompt")

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_missing_content(self):
        """build_llm_caller() raises RuntimeError when 'content' key is missing."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"role": "assistant"}}]
        }
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            with pytest.raises(RuntimeError, match="unexpected LLM response shape"):
                await caller("some prompt")

    @pytest.mark.asyncio
    async def test_uses_env_url_when_base_url_not_provided(self):
        """build_llm_caller() uses LLAMA_CPP_URL env var when base_url is None."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            with patch(
                "chat.summarizer.os.environ.get", return_value="http://env:9090"
            ):
                caller = build_llm_caller()
                await caller("prompt")

        call_url = mock_instance.post.call_args[0][0]
        assert "http://env:9090" in call_url

    @pytest.mark.asyncio
    async def test_propagates_non_retryable_http_error(self):
        """build_llm_caller() propagates non-retryable HTTP errors immediately."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 400
        error = httpx.HTTPStatusError(
            "400 Bad Request",
            request=MagicMock(),
            response=mock_response,
        )
        mock_response.raise_for_status.side_effect = error
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("chat.summarizer.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            with pytest.raises(httpx.HTTPStatusError):
                await caller("some prompt", max_retries=0)

    @pytest.mark.asyncio
    async def test_retries_on_transient_http_errors(self):
        """build_llm_caller() retries on 502/503/504 then returns on success."""
        mock_instance = MagicMock()

        fail_response = MagicMock()
        fail_response.status_code = 504
        fail_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "504 Gateway Timeout",
            request=MagicMock(),
            response=fail_response,
        )

        ok_response = MagicMock()
        ok_response.raise_for_status = MagicMock()
        ok_response.json.return_value = {
            "choices": [{"message": {"content": "Retried OK"}}]
        }

        mock_instance.post = AsyncMock(side_effect=[fail_response, ok_response])

        with (
            patch("chat.summarizer.httpx.AsyncClient") as mock_cls,
            patch("chat.summarizer.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            result = await caller("some prompt", max_retries=2)

        assert result == "Retried OK"
        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_retries(self):
        """build_llm_caller() raises after all retries are exhausted."""
        mock_instance = MagicMock()

        fail_response = MagicMock()
        fail_response.status_code = 502
        fail_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "502 Bad Gateway",
            request=MagicMock(),
            response=fail_response,
        )
        mock_instance.post = AsyncMock(return_value=fail_response)

        with (
            patch("chat.summarizer.httpx.AsyncClient") as mock_cls,
            patch("chat.summarizer.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value = mock_instance
            caller = build_llm_caller("http://fake:8080")
            with pytest.raises(httpx.HTTPStatusError, match="502"):
                await caller("some prompt", max_retries=2)

        # 1 initial + 2 retries = 3 total attempts
        assert mock_instance.post.call_count == 3


# ---------------------------------------------------------------------------
# TestGenerateSummariesErrorHandling -- per-pair error isolation
# ---------------------------------------------------------------------------


class TestGenerateSummariesErrorHandling:
    @pytest.mark.asyncio
    async def test_continues_to_next_pair_when_one_raises(self, session):
        """generate_summaries() skips a failing pair and processes the next one."""
        _make_message(session, "ch1", "u1", "Alice", "Hello world", 1)
        _make_message(session, "ch2", "u2", "Bob", "Goodbye world", 2)

        call_order = []

        async def flaky_llm(prompt: str) -> str:
            if "Alice" in prompt:
                call_order.append("alice_fail")
                raise RuntimeError("LLM exploded for Alice")
            call_order.append("bob_ok")
            return "Bob talked about goodbyes."

        await generate_summaries(session, flaky_llm)

        # Alice's summary should not have been created (exception was raised)
        alice_summary = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.user_id == "u1")
        ).first()
        assert alice_summary is None

        # Bob's summary should have been created despite Alice's failure
        bob_summary = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.user_id == "u2")
        ).first()
        assert bob_summary is not None
        assert bob_summary.summary == "Bob talked about goodbyes."

    @pytest.mark.asyncio
    async def test_processes_multiple_channel_user_pairs(self, session):
        """generate_summaries() processes all (channel, user) pairs in one call."""
        _make_message(session, "ch1", "u1", "Alice", "First message", 1)
        _make_message(session, "ch1", "u2", "Bob", "Second message", 2)
        _make_message(session, "ch2", "u1", "Alice", "Third message", 3)

        summaries_generated = []

        async def mock_llm(prompt: str) -> str:
            summary = "summary for prompt"
            summaries_generated.append(prompt)
            return summary

        await generate_summaries(session, mock_llm)

        # Three unique (channel, user) pairs: (ch1, u1), (ch1, u2), (ch2, u1)
        all_summaries = session.exec(select(UserChannelSummary)).all()
        assert len(all_summaries) == 3
        assert len(summaries_generated) == 3

    @pytest.mark.asyncio
    async def test_all_pairs_fail_does_not_raise(self, session):
        """generate_summaries() completes without raising even when all pairs fail."""
        _make_message(session, "ch1", "u1", "Alice", "Message", 1)

        async def always_fail(prompt: str) -> str:
            raise RuntimeError("always fails")

        # Should not propagate the exception
        await generate_summaries(session, always_fail)

        summaries = session.exec(select(UserChannelSummary)).all()
        assert len(summaries) == 0
