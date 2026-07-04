"""Tests for chat.observer -- style-friction classifier (ADR 035 phase 4)."""

import json

import pytest

from chat.observer import find_style_friction


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


class _RaisingCaller:
    async def __call__(self, prompt: str) -> str:
        raise RuntimeError("caller boom")


def _exchange(message_id: str, text: str = "text", author: str = "joe") -> dict:
    return {"message_id": message_id, "author": author, "text": text}


def _friction_reply(
    ids: list[str], directive_change: str = "reply more concisely"
) -> str:
    return json.dumps(
        {
            "friction": True,
            "directive_change": directive_change,
            "evidence_message_ids": ids,
        }
    )


class TestEmptyExchanges:
    @pytest.mark.asyncio
    async def test_empty_exchanges_returns_none_without_calling_caller(self):
        caller = _FakeCaller()
        result = await find_style_friction([], caller)
        assert result is None
        assert caller.prompts == []


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_recurring_friction_returns_directive_change_and_evidence(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(responses=[_friction_reply(["m1", "m2", "m3"])])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result == {
            "directive_change": "reply more concisely",
            "evidence_message_ids": ["m1", "m2", "m3"],
        }

    @pytest.mark.asyncio
    async def test_prompt_lists_message_ids_authors_and_text(self):
        exchanges = [_exchange("m1", text="too long!", author="joe")]
        caller = _FakeCaller(responses=[_friction_reply(["m1"])])
        await find_style_friction(exchanges, caller, min_evidence=1)
        prompt = caller.prompts[0]
        assert "m1: joe: too long!" in prompt

    @pytest.mark.asyncio
    async def test_prompt_restricts_directive_change_to_tone_never_tools_or_access(
        self,
    ):
        exchanges = [_exchange("m1")]
        caller = _FakeCaller(responses=[_friction_reply(["m1"])])
        await find_style_friction(exchanges, caller, min_evidence=1)
        prompt = caller.prompts[0].lower()
        assert "tone" in prompt
        assert "attention" in prompt
        assert "interaction style" in prompt
        assert "never" in prompt
        assert "tool" in prompt
        assert "permission" in prompt
        assert "ambient" in prompt
        assert "repo" in prompt

    @pytest.mark.asyncio
    async def test_prompt_requires_recurring_not_single_complaint(self):
        exchanges = [_exchange("m1")]
        caller = _FakeCaller(responses=[_friction_reply(["m1"])])
        await find_style_friction(exchanges, caller, min_evidence=1)
        prompt = caller.prompts[0].lower()
        assert "recurring" in prompt
        assert "not recurring friction" in prompt or "not" in prompt

    @pytest.mark.asyncio
    async def test_evidence_ids_deduplicated_order_preserved(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(
            responses=[_friction_reply(["m2", "m1", "m2", "m3", "m1"])]
        )
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result["evidence_message_ids"] == ["m2", "m1", "m3"]


class TestFrictionFalse:
    @pytest.mark.asyncio
    async def test_friction_false_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        reply = json.dumps(
            {
                "friction": False,
                "directive_change": "",
                "evidence_message_ids": [],
            }
        )
        caller = _FakeCaller(responses=[reply])
        result = await find_style_friction(exchanges, caller)
        assert result is None


class TestInsufficientEvidence:
    @pytest.mark.asyncio
    async def test_fewer_than_min_evidence_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(responses=[_friction_reply(["m1", "m2"])])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None

    @pytest.mark.asyncio
    async def test_dedup_below_min_evidence_after_dedup_returns_none(self):
        # Three raw ids but only two distinct ones after dedup: below min_evidence=3.
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(responses=[_friction_reply(["m1", "m1", "m2"])])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None


class TestHallucinatedEvidence:
    @pytest.mark.asyncio
    async def test_evidence_id_not_in_input_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(responses=[_friction_reply(["m1", "m2", "made-up-id"])])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None


class TestEmptyDirectiveChange:
    @pytest.mark.asyncio
    async def test_missing_directive_change_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        reply = json.dumps(
            {"friction": True, "evidence_message_ids": ["m1", "m2", "m3"]}
        )
        caller = _FakeCaller(responses=[reply])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None

    @pytest.mark.asyncio
    async def test_blank_directive_change_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(
            responses=[_friction_reply(["m1", "m2", "m3"], directive_change="   ")]
        )
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None


class TestUnparseableReply:
    @pytest.mark.asyncio
    async def test_non_json_reply_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        caller = _FakeCaller(responses=["not json at all"])
        result = await find_style_friction(exchanges, caller, min_evidence=3)
        assert result is None


class TestCallerException:
    @pytest.mark.asyncio
    async def test_caller_exception_returns_none(self):
        exchanges = [_exchange("m1"), _exchange("m2"), _exchange("m3")]
        result = await find_style_friction(exchanges, _RaisingCaller(), min_evidence=3)
        assert result is None
