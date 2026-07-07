"""Tests for chat.reply_sanitize: the tool-call leak shield.

scrub_tool_leak strips leaked <tool_call>/<arg_*> scaffolding and markdown image
tags, reporting whether tool-call markers were present. repair_leaked_reply
always scrubs and, only on a detected leak, runs a bounded model-repair loop
(injected caller) to recover a clean answer, falling back to the scrub.
"""

from unittest.mock import AsyncMock

import pytest

from chat.reply_sanitize import repair_leaked_reply, scrub_tool_leak

# A leak shaped like the real one: a closed tool_call wrapping arg blocks + code.
_LEAK = (
    "<tool_call><arg_key>code</arg_key><arg_value>import matplotlib.pyplot as plt\n"
    "plt.savefig('chart.png')</arg_value></tool_call>"
)


class TestScrub:
    def test_clean_text_passes_through_unflagged(self):
        cleaned, leaked = scrub_tool_leak("BMWs are overweight, but fun.")
        assert cleaned == "BMWs are overweight, but fun."
        assert leaked is False

    def test_empty(self):
        assert scrub_tool_leak("") == ("", False)

    def test_closed_tool_call_block_stripped_and_flagged(self):
        cleaned, leaked = scrub_tool_leak("Here you go.\n" + _LEAK)
        assert leaked is True
        assert "<tool_call>" not in cleaned
        assert "savefig" not in cleaned
        assert cleaned == "Here you go."

    def test_truncated_unclosed_tool_call_stripped_to_end(self):
        raw = "The answer is 42.\n<tool_call><arg_key>code</arg_key><arg_value>x = 1"
        cleaned, leaked = scrub_tool_leak(raw)
        assert leaked is True
        assert cleaned == "The answer is 42."

    def test_arg_blocks_without_wrapper_stripped(self):
        raw = "<arg_key>code</arg_key><arg_value>print(1)</parameter>"
        cleaned, leaked = scrub_tool_leak(raw)
        assert leaked is True
        assert "print(1)" not in cleaned
        assert "<arg" not in cleaned

    def test_orphan_tags_removed(self):
        cleaned, leaked = scrub_tool_leak("text </parameter></tool_call>")
        assert leaked is True
        assert "<" not in cleaned
        assert cleaned == "text"

    def test_markdown_image_stripped_without_leak_flag(self):
        # A stray markdown image is cosmetic: strip it, but do NOT flag a leak
        # (so it never triggers the model-repair loop).
        cleaned, leaked = scrub_tool_leak("See the chart ![c](chart.png) attached")
        assert leaked is False
        assert "![" not in cleaned
        assert "chart.png" not in cleaned


class TestRepair:
    @pytest.mark.asyncio
    async def test_clean_reply_skips_the_model(self):
        caller = AsyncMock()
        out = await repair_leaked_reply("All good here.", llm_call=caller)
        assert out.leaked is False
        assert out.attempts == 0
        assert out.final == "All good here."
        assert out.outcome == "clean"
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_leak_repaired_by_model(self):
        caller = AsyncMock(return_value="The M3 out-torques the STI across the board.")
        out = await repair_leaked_reply("Here.\n" + _LEAK, llm_call=caller, max_turns=2)
        assert out.leaked is True
        assert out.attempts == 1
        assert out.still_dirty is False
        assert out.final == "The M3 out-torques the STI across the board."
        assert out.outcome == "clean_after_repair"
        caller.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leak_model_keeps_leaking_falls_back_to_scrub(self):
        # Every repair pass still leaks: exhaust max_turns, deliver the scrub.
        caller = AsyncMock(return_value="still dirty " + _LEAK)
        out = await repair_leaked_reply(
            "Prose.\n" + _LEAK, llm_call=caller, max_turns=2
        )
        assert out.leaked is True
        assert out.attempts == 2
        assert out.still_dirty is True
        assert out.final == "Prose."  # scrub floor
        assert out.outcome == "still_dirty"
        assert caller.await_count == 2

    @pytest.mark.asyncio
    async def test_model_failure_falls_back_to_scrub(self):
        caller = AsyncMock(side_effect=RuntimeError("model down"))
        out = await repair_leaked_reply(
            "Prose.\n" + _LEAK, llm_call=caller, max_turns=2
        )
        assert out.leaked is True
        assert out.attempts == 0
        assert out.still_dirty is True
        assert out.final == "Prose."
        assert out.outcome == "still_dirty"

    @pytest.mark.asyncio
    async def test_markdown_image_only_does_not_trigger_repair(self):
        caller = AsyncMock()
        out = await repair_leaked_reply("See ![c](c.png) below", llm_call=caller)
        assert out.leaked is False
        assert "![" not in out.final
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_turns_zero_scrubs_only(self):
        caller = AsyncMock()
        out = await repair_leaked_reply("Hi.\n" + _LEAK, llm_call=caller, max_turns=0)
        assert out.leaked is True
        assert out.attempts == 0
        assert out.final == "Hi."
        caller.assert_not_called()
