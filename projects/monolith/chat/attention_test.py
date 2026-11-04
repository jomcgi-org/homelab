"""Tests for chat.attention: the attention gate (ADR 035 phase 3).

Mentions/replies engage without touching the classifier; ambient channels
classify via an injected fast-model caller; the classifier fails closed on
any error and tolerates stray text around its JSON reply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat.attention as attention_mod
from chat.attention import ATTENTION_THRESHOLD, evaluate, needs_agent, should_send


def _make_message(content: str = "hey", mentions=None, reference=None):
    return SimpleNamespace(
        content=content,
        mentions=mentions if mentions is not None else [],
        reference=reference,
        author=SimpleNamespace(bot=False, id=1),
    )


_BOT_USER = SimpleNamespace(id=999)


def _payload(prompt, start, end):
    """Read one framed input independently of the classifier policy prose."""
    assert prompt.count(start) == 1
    _, _, tail = prompt.partition(start)
    payload, separator, _ = tail.partition(end)
    assert separator
    return payload


def _boundary_cases(limit, fill, final, excluded):
    return [
        (None, ""),
        ("", ""),
        (fill * (limit - 2) + final, fill * (limit - 2) + final),
        (fill * (limit - 1) + final, fill * (limit - 1) + final),
        (fill * (limit - 1) + final + excluded, fill * (limit - 1) + final),
    ]


class TestExplicitTrigger:
    @pytest.mark.asyncio
    async def test_mention_engages_without_calling_the_classifier(self):
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message, "", _BOT_USER, is_ambient=False, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 1.0
        # A hard @mention is an explicit summons: the caller keeps it on the
        # live, never-suppressed reply path even in an ambient channel.
        assert result.explicit is True
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_in_ambient_channel_is_explicit(self):
        # The bug this guards: a direct @mention inside an ambient channel must
        # still be flagged explicit so the no_reply tool / send-gate cannot eat
        # it (the mention short-circuits before the classifier either way).
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message,
            "only jump in when @-mentioned",
            _BOT_USER,
            is_ambient=True,
            _caller=caller,
        )
        assert result.engage is True
        assert result.explicit is True
        caller.assert_not_called()


class TestNonAmbientNonMention:
    @pytest.mark.asyncio
    async def test_ignored_without_calling_the_classifier(self):
        message = _make_message()
        caller = AsyncMock()
        result = await evaluate(
            message, "", _BOT_USER, is_ambient=False, _caller=caller
        )
        assert result.engage is False
        assert result.confidence == 0.0
        caller.assert_not_called()


class TestAmbientClassification:
    @pytest.mark.asyncio
    async def test_engages_above_threshold(self):
        message = _make_message(content="hey can you help with this")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.9}')
        result = await evaluate(
            message, "help with code", _BOT_USER, is_ambient=True, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 0.9
        # A soft classifier engage is NOT an explicit summons, so it stays on
        # the suppressible ambient path (no_reply tool / send-gate can veto it).
        assert result.explicit is False
        caller.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_below_threshold(self):
        message = _make_message(content="maybe you could help")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.3}')
        result = await evaluate(
            message, "help with code", _BOT_USER, is_ambient=True, _caller=caller
        )
        assert result.confidence < ATTENTION_THRESHOLD
        assert result.engage is False

    @pytest.mark.asyncio
    async def test_fails_closed_on_caller_error(self):
        message = _make_message(content="anything")
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        result = await evaluate(message, "", _BOT_USER, is_ambient=True, _caller=caller)
        assert result.engage is False
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        message = _make_message(content="anything")
        caller = AsyncMock(return_value='sure! {"engage": true, "confidence": 0.95} ok')
        result = await evaluate(message, "", _BOT_USER, is_ambient=True, _caller=caller)
        assert result.engage is True
        assert result.confidence == 0.95


class TestRecentTagWeighting:
    """ADR 035 engagement policy: recently_tagged lowers the engage threshold."""

    @pytest.mark.asyncio
    async def test_recently_tagged_engages_below_default_threshold(self):
        message = _make_message(content="is that actually true though?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.4}')
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=True,
            recently_tagged=True,
            _caller=caller,
        )
        assert result.confidence == 0.4
        assert result.confidence < ATTENTION_THRESHOLD
        assert result.engage is True

    @pytest.mark.asyncio
    async def test_same_confidence_ignored_without_recent_tag(self):
        message = _make_message(content="is that actually true though?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.4}')
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=True,
            recently_tagged=False,
            _caller=caller,
        )
        assert result.confidence == 0.4
        assert result.engage is False

    @pytest.mark.asyncio
    async def test_mention_still_short_circuits_without_a_caller(self):
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=False,
            recently_tagged=True,
            _caller=caller,
        )
        assert result.engage is True
        assert result.confidence == 1.0
        caller.assert_not_called()


class TestEvaluateInputContract:
    """Scripted responses test input transport and decisions, not model judgment."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", [True, False])
    @pytest.mark.parametrize(
        "directive", [None, "", "[evaluate-guidance]" + "指" * 2501]
    )
    @pytest.mark.parametrize(
        ("content", "expected"),
        _boundary_cases(500, "α", "β", "γ"),
        ids=["none", "empty", "499", "500", "501"],
    )
    async def test_message_and_directive_contract(
        self, content, expected, directive, decision
    ):
        caller = AsyncMock(
            return_value=f'{{"engage": {str(decision).lower()}, "confidence": 0.9}}'
        )
        result = await evaluate(
            _make_message(content=content),
            directive,
            _BOT_USER,
            is_ambient=True,
            _caller=caller,
        )
        caller.assert_awaited_once()
        (prompt,) = caller.await_args.args
        assert caller.await_args.kwargs == {}
        assert (
            _payload(prompt, "Message: ", "\nReply with ONLY a JSON object:")
            == expected
        )
        guidance = "Channel guidance (adjust the above if it applies): "
        if directive:
            assert _payload(prompt, guidance, "\nMessage: ") == directive
        else:
            assert guidance not in prompt
        assert result.engage is decision
        assert result.confidence == 0.9
        assert result.explicit is False


class TestSendGate:
    """Post-generation send-gate (improve-ambient): a disconnected classify that
    reads the drafted reply and vetoes an ambient send that would misfire.
    Fails open; skipped when disabled."""

    @pytest.mark.asyncio
    async def test_send_true_passes(self):
        caller = AsyncMock(return_value='{"send": true}')
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)
        caller.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_false_vetoes(self):
        caller = AsyncMock(return_value='{"send": false}')
        assert (
            await should_send("d", "convo", "trigger", "reply", _caller=caller) is False
        )

    @pytest.mark.asyncio
    async def test_fails_open_on_caller_error(self):
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)

    @pytest.mark.asyncio
    async def test_missing_key_defaults_to_send(self):
        caller = AsyncMock(return_value="{}")
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        caller = AsyncMock(return_value='hold on {"send": false} ok')
        assert (
            await should_send("d", "convo", "trigger", "reply", _caller=caller) is False
        )

    @pytest.mark.asyncio
    async def test_disabled_returns_true_without_calling(self, monkeypatch):
        monkeypatch.setattr(attention_mod, "SEND_GATE_ENABLED", False)
        caller = AsyncMock()
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)
        caller.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", [True, False])
    @pytest.mark.parametrize("directive", [None, "", "[send-guidance]" + "導" * 2501])
    @pytest.mark.parametrize(
        ("slot", "content", "expected"),
        [
            (slot, content, expected)
            for slot, limit, fill, final, excluded in [
                ("conversation", 2000, "会", "話", "外"),
                ("trigger", 500, "問", "題", "除"),
                ("reply", 1000, "返", "答", "余"),
            ]
            for content, expected in _boundary_cases(limit, fill, final, excluded)
        ],
    )
    async def test_prompt_input_and_response_contract(
        self, slot, content, expected, directive, decision
    ):
        # Each input has its own sentinel and framing. A conversation cannot
        # satisfy the trigger assertion, even when both inputs are present.
        inputs = {
            "conversation": "[conversation-only]",
            "trigger": "[trigger-only]",
            "reply": "[reply-only]",
        }
        expected_inputs = dict(inputs)
        inputs[slot] = content
        expected_inputs[slot] = expected
        caller = AsyncMock(return_value=f'{{"send": {str(decision).lower()}}}')
        result = await should_send(directive, **inputs, _caller=caller)
        caller.assert_awaited_once()
        (prompt,) = caller.await_args.args
        assert caller.await_args.kwargs == {}
        guidance = "Channel guidance (weigh this heavily): "
        if directive:
            assert _payload(prompt, guidance, "\nRecent conversation:\n") == directive
        else:
            assert guidance not in prompt
        assert (
            _payload(
                prompt,
                "Recent conversation:\n",
                "\nLatest message Bosun is replying to: ",
            )
            == expected_inputs["conversation"]
        )
        assert (
            _payload(
                prompt,
                "\nLatest message Bosun is replying to: ",
                "\nBosun's drafted reply: ",
            )
            == expected_inputs["trigger"]
        )
        assert (
            _payload(
                prompt, "\nBosun's drafted reply: ", "\nReply with ONLY a JSON object:"
            )
            == expected_inputs["reply"]
        )
        assert result is decision


class TestNeedsAgent:
    """ADR 035 Phase 4: the in-monolith depth classify (chat vs goose guest)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", [True, False], ids=["true", "false"])
    @pytest.mark.parametrize(
        ("content", "expected_payload", "excluded"),
        [
            (None, "", ""),
            ("", "", ""),
            ("[payload-one]", "[payload-one]", ""),
            ("[payload-two]", "[payload-two]", ""),
            ("α" * 498 + "β", "α" * 498 + "β", ""),
            ("α" * 498 + "βγ", "α" * 498 + "βγ", ""),
            ("α" * 498 + "βγδ", "α" * 498 + "βγ", "δ"),
        ],
        ids=["none", "empty", "short-one", "short-two", "499", "500", "501"],
    )
    async def test_classifier_input_and_response_contract(
        self, content, expected_payload, excluded, decision
    ):
        message = _make_message(content=content)
        caller = AsyncMock(return_value=f'{{"needs_agent": {str(decision).lower()}}}')
        result = await needs_agent(message, _caller=caller)
        caller.assert_awaited_once()
        (prompt,) = caller.await_args.args
        # Inspect the message field, including an actually empty payload.
        # The policy prose before this framing is free to change.
        _, separator, payload = prompt.rpartition(" Message: ")
        assert separator
        assert payload == expected_payload
        if excluded:
            assert excluded not in prompt
        assert result is decision

    @pytest.mark.asyncio
    async def test_fails_closed_to_chat_on_caller_error(self):
        message = _make_message(content="anything")
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        result = await needs_agent(message, _caller=caller)
        assert result is False

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        message = _make_message(content="anything")
        caller = AsyncMock(return_value='sure! {"needs_agent": true} ok')
        result = await needs_agent(message, _caller=caller)
        assert result is True


class TestDirectedSeam:
    """The non-Discord `directed` seam (ADR 039): a caller-supplied directedness
    signal engages without the Discord mention check, and a None bot_user (a
    non-Discord channel) never reaches should_respond."""

    @pytest.mark.asyncio
    async def test_directed_engages_without_caller_or_bot_user(self):
        message = _make_message(content="hey bosun")
        caller = AsyncMock()
        result = await evaluate(
            message, "", None, is_ambient=False, directed=True, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 1.0
        # A caller-supplied directedness signal (reply-to-bot / trigger-name) is
        # also an explicit summons.
        assert result.explicit is True
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_bot_user_non_directed_non_ambient_ignores(self):
        # A non-Discord message that is neither directed nor ambient is ignored
        # without touching should_respond (which would fail on a non-Discord shape).
        message = _make_message(content="just the two of us")
        caller = AsyncMock()
        result = await evaluate(
            message, "", None, is_ambient=False, directed=False, _caller=caller
        )
        assert result.engage is False
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_bot_user_ambient_still_classifies(self):
        message = _make_message(content="anyone around?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.9}')
        result = await evaluate(
            message, "household", None, is_ambient=True, directed=False, _caller=caller
        )
        assert result.engage is True
        caller.assert_called_once()
