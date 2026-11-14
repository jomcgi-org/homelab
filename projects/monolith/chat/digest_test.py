"""Tests for chat.digest -- chunked window digest (summary/decisions modes)."""

from datetime import datetime, timedelta, timezone

import pytest

from chat import digest
from chat.digest import digest_window
from chat.models import Message


class _FakeCaller:
    """Records every prompt it is called with; returns canned responses in
    order, falling back to a labelled placeholder once they run out."""

    def __init__(self, responses: list[str] | None = None):
        self.prompts: list[str] = []
        self._responses = list(responses) if responses is not None else None

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._responses:
            return self._responses.pop(0)
        return f"response-{len(self.prompts)}"


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


class TestEmptyWindow:
    @pytest.mark.asyncio
    async def test_empty_messages_returns_plain_string_without_calling_caller(self):
        caller = _FakeCaller()
        result = await digest_window([], "summary", caller)
        assert result == "No messages in the window."
        assert caller.prompts == []


class TestSingleCallWindow:
    @pytest.mark.asyncio
    async def test_formats_messages_as_time_username_content_lines(self, base_time):
        messages = [
            _make_message(
                username="Alice", content="hello there", minutes_ago=5, base=base_time
            ),
            _make_message(
                username="Bob", content="hi back", minutes_ago=1, base=base_time
            ),
        ]
        caller = _FakeCaller()
        await digest_window(messages, "summary", caller)
        assert len(caller.prompts) == 1
        prompt = caller.prompts[0]
        assert "[11:55] Alice: hello there" in prompt
        assert "[11:59] Bob: hi back" in prompt

    @pytest.mark.asyncio
    async def test_window_under_budget_makes_exactly_one_call(self, base_time):
        messages = [
            _make_message(
                username="Alice", content=f"msg {i}", minutes_ago=i, base=base_time
            )
            for i in range(10)
        ]
        caller = _FakeCaller()
        await digest_window(messages, "summary", caller)
        assert len(caller.prompts) == 1

    @pytest.mark.asyncio
    async def test_returned_string_starts_with_coverage_line(self, base_time):
        messages = [
            _make_message(
                username="Alice", content="a", minutes_ago=30, base=base_time
            ),
            _make_message(username="Bob", content="b", minutes_ago=0, base=base_time),
        ]
        caller = _FakeCaller(responses=["the digest body"])
        result = await digest_window(messages, "summary", caller)
        oldest_iso = messages[0].created_at.isoformat()
        assert result.startswith(f"(window: 2 messages, back to {oldest_iso})")
        assert "the digest body" in result


class TestChunkedWindow:
    @pytest.mark.asyncio
    async def test_large_window_chunks_then_reduces_with_one_final_call(
        self, base_time
    ):
        # Each line is ~3015 chars. The first two together (~6032) fit under the
        # 8000-char chunk budget, but the third pushes past it, so this splits
        # into exactly two chunks (2 messages, then 1) plus one reduce call.
        messages = [
            _make_message(
                username="Alice", content="x" * 3000, minutes_ago=i, base=base_time
            )
            for i in range(3)
        ]
        caller = _FakeCaller(
            responses=["chunk summary one", "chunk summary two", "final digest"]
        )
        result = await digest_window(messages, "summary", caller)

        # 2 chunk calls + 1 reduce call.
        assert len(caller.prompts) == 3
        reduce_prompt = caller.prompts[-1]
        assert "chunk summary one" in reduce_prompt
        assert "chunk summary two" in reduce_prompt
        assert "final digest" in result


@pytest.fixture(params=["single", "chunked"])
def routing_window(request, base_time):
    # Three distinct chronological messages split 2/1 at the real 8000-char
    # budget. Expected payloads are independent of digest's formatting helpers.
    chunked = request.param == "chunked"
    contents = ["launch-" + "a" * 3000, "owner-" + "b" * 3000, "date-" + "c" * 3000]
    if not chunked:
        contents = ["launch approved", "owner is Bob", "date still open"]
    messages = [
        _make_message(
            username=name, content=content, minutes_ago=minutes, base=base_time
        )
        for name, content, minutes in zip(
            ["Alice", "Bob", "Carol"], contents, [9, 5, 1], strict=True
        )
    ]
    lines = [
        "[11:51] Alice: " + contents[0],
        "[11:55] Bob: " + contents[1],
        "[11:59] Carol: " + contents[2],
    ]
    payloads = ["\n".join(lines)]
    responses = ["final caller output"]
    if chunked:
        payloads = ["\n".join(lines[:2]), lines[2]]
        responses = ["launch partial alpha", "ownership partial beta", *responses]
    return messages, payloads, responses


@pytest.mark.parametrize("mode", ["summary", "decisions"])
@pytest.mark.asyncio
async def test_prompt_routing(mode, routing_window, monkeypatch):
    # Patch every mode/stage, including unused alternatives, so wrong routing
    # cannot accidentally select an equivalent template. pytest restores them.
    monkeypatch.setattr(
        digest,
        "_MODE_PROMPTS",
        {
            name: {
                "single": f"{name}/single<{{messages}}>",
                "chunk": f"{name}/chunk<{{messages}}>",
                "reduce": f"{name}/reduce<{{chunks}}>",
            }
            for name in ("summary", "decisions")
        },
    )
    messages, payloads, responses = routing_window
    caller = _FakeCaller(responses=responses)
    result = await digest_window(messages, mode, caller)

    if len(payloads) == 1:
        expected = [f"{mode}/single<{payloads[0]}>"]
    else:
        expected = [f"{mode}/chunk<{payload}>" for payload in payloads]
        expected.append(
            f"{mode}/reduce<--- Piece 1 ---\nlaunch partial alpha"
            "\n\n--- Piece 2 ---\nownership partial beta>"
        )
    assert len(caller.prompts) == len(expected)
    assert caller.prompts == expected
    assert result == (
        "(window: 3 messages, back to 2026-07-04T11:51:00+00:00)\n\nfinal caller output"
    )


@pytest.mark.parametrize("mode", ["summary", "decisions"])
@pytest.mark.asyncio
async def test_production_templates_preserve_payloads(mode, routing_window):
    # Keep the real templates: sentinel substitution alone hides missing real
    # placeholders. Assert data flow without copying or pinning prompt prose.
    messages, payloads, responses = routing_window
    caller = _FakeCaller(responses=responses)
    result = await digest_window(messages, mode, caller)

    chunked = len(payloads) == 2
    assert len(caller.prompts) == (3 if chunked else 1)
    for index, payload in enumerate(payloads):
        assert payload in caller.prompts[index]
        for other in payloads[:index] + payloads[index + 1 :]:
            assert other not in caller.prompts[index]
    if chunked:
        assert (
            "--- Piece 1 ---\nlaunch partial alpha"
            "\n\n--- Piece 2 ---\nownership partial beta"
        ) in caller.prompts[2]
        for payload in payloads:
            assert payload not in caller.prompts[2]
    assert result == (
        "(window: 3 messages, back to 2026-07-04T11:51:00+00:00)\n\nfinal caller output"
    )


class TestInvalidMode:
    @pytest.mark.asyncio
    async def test_unknown_mode_raises_without_calling_caller(self, base_time):
        messages = [
            _make_message(username="Alice", content="hi", minutes_ago=0, base=base_time)
        ]
        caller = _FakeCaller()
        with pytest.raises(ValueError):
            await digest_window(messages, "bogus", caller)
        assert caller.prompts == []
