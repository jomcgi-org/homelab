"""Tests for chat.channel_data -- structured dataset extraction."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from chat.channel_data import MAX_ROWS, extract_dataset
from chat.models import Message


class _FakeCaller:
    """Records every prompt it is called with; returns a canned response."""

    def __init__(self, response: str = "{}"):
        self.prompts: list[str] = []
        self._response = response

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class _RaisingCaller:
    async def __call__(self, prompt: str) -> str:
        raise RuntimeError("caller boom")


def _make_message(
    *, username: str, content: str, minutes_ago: int, base: datetime
) -> Message:
    return Message(
        discord_message_id=f"{minutes_ago}",
        channel_id="ch1",
        user_id=username.lower(),
        username=username,
        content=content,
        is_bot=False,
        embedding=[],
        created_at=base - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def base_time():
    return datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def messages(base_time):
    return [
        _make_message(
            username="Alice",
            content="we shipped 3 features",
            minutes_ago=5,
            base=base_time,
        ),
        _make_message(
            username="Bob", content="and fixed 2 bugs", minutes_ago=1, base=base_time
        ),
    ]


_VALID_REPLY = json.dumps(
    {
        "title": "Weekly counts",
        "columns": ["person", "count"],
        "rows": [["Alice", 3], ["Bob", 2]],
    }
)


class TestEmptyWindow:
    @pytest.mark.asyncio
    async def test_empty_messages_returns_none_without_calling_caller(self):
        caller = _FakeCaller(_VALID_REPLY)
        result = await extract_dataset([], "count features", caller)
        assert result is None
        assert caller.prompts == []


class TestSuccessfulExtraction:
    @pytest.mark.asyncio
    async def test_prompt_includes_formatted_messages_and_request(self, messages):
        caller = _FakeCaller(_VALID_REPLY)
        await extract_dataset(messages, "count features per person", caller)
        assert len(caller.prompts) == 1
        prompt = caller.prompts[0]
        assert "[11:55] Alice: we shipped 3 features" in prompt
        assert "[11:59] Bob: and fixed 2 bugs" in prompt
        assert "count features per person" in prompt

    @pytest.mark.asyncio
    async def test_returns_json_string_with_title_columns_rows(self, messages):
        caller = _FakeCaller(_VALID_REPLY)
        result = await extract_dataset(messages, "count features", caller)
        assert result is not None
        data = json.loads(result)
        assert data["title"] == "Weekly counts"
        assert data["columns"] == ["person", "count"]
        assert data["rows"] == [["Alice", 3], ["Bob", 2]]

    @pytest.mark.asyncio
    async def test_source_window_computed_from_input_messages_not_model(
        self, messages, base_time
    ):
        # A malicious/confused reply claiming a different window is ignored:
        # source_window must reflect the actual input messages.
        reply = json.dumps(
            {
                "title": "t",
                "columns": ["a"],
                "rows": [["1"]],
                "source_window": {
                    "messages": 999,
                    "oldest": "2000-01-01T00:00:00+00:00",
                },
            }
        )
        caller = _FakeCaller(reply)
        result = await extract_dataset(messages, "req", caller)
        data = json.loads(result)
        assert data["source_window"]["messages"] == len(messages)
        assert data["source_window"]["oldest"] == messages[0].created_at.isoformat()

    @pytest.mark.asyncio
    async def test_strips_markdown_code_fences_before_parsing(self, messages):
        fenced = f"```json\n{_VALID_REPLY}\n```"
        caller = _FakeCaller(fenced)
        result = await extract_dataset(messages, "count features", caller)
        assert result is not None
        data = json.loads(result)
        assert data["title"] == "Weekly counts"

    @pytest.mark.asyncio
    async def test_tolerates_stray_preamble_text_around_json(self, messages):
        chatty = f"Sure, here you go:\n{_VALID_REPLY}\nHope that helps!"
        caller = _FakeCaller(chatty)
        result = await extract_dataset(messages, "count features", caller)
        assert result is not None
        data = json.loads(result)
        assert data["title"] == "Weekly counts"


class TestValidationFailures:
    @pytest.mark.asyncio
    async def test_unparseable_json_returns_none(self, messages):
        caller = _FakeCaller("not json at all")
        result = await extract_dataset(messages, "req", caller)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_title_returns_none(self, messages):
        reply = json.dumps({"columns": ["a"], "rows": [["1"]]})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_title_returns_none(self, messages):
        reply = json.dumps({"title": "", "columns": ["a"], "rows": [["1"]]})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_columns_returns_none(self, messages):
        reply = json.dumps({"title": "t", "rows": [["1"]]})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_columns_returns_none(self, messages):
        reply = json.dumps({"title": "t", "columns": [], "rows": []})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_non_string_column_returns_none(self, messages):
        reply = json.dumps({"title": "t", "columns": ["a", 1], "rows": []})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_row_length_mismatch_returns_none(self, messages):
        reply = json.dumps(
            {"title": "t", "columns": ["a", "b"], "rows": [["1", "2", "3"]]}
        )
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_row_not_a_list_returns_none(self, messages):
        reply = json.dumps({"title": "t", "columns": ["a"], "rows": ["not-a-row"]})
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_too_many_rows_returns_none(self, messages):
        reply = json.dumps(
            {
                "title": "t",
                "columns": ["a"],
                "rows": [[str(i)] for i in range(MAX_ROWS + 1)],
            }
        )
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is None

    @pytest.mark.asyncio
    async def test_exactly_max_rows_is_accepted(self, messages):
        reply = json.dumps(
            {
                "title": "t",
                "columns": ["a"],
                "rows": [[str(i)] for i in range(MAX_ROWS)],
            }
        )
        result = await extract_dataset(messages, "req", _FakeCaller(reply))
        assert result is not None

    @pytest.mark.asyncio
    async def test_reply_that_is_a_json_list_not_object_returns_none(self, messages):
        result = await extract_dataset(messages, "req", _FakeCaller("[1, 2, 3]"))
        assert result is None


class TestCallerFailsOpen:
    @pytest.mark.asyncio
    async def test_caller_exception_returns_none_not_raise(self, messages):
        result = await extract_dataset(messages, "req", _RaisingCaller())
        assert result is None
