"""Tests for temporal grounding (today's date + knowledge cutoff) injection.

The model was confidently calling true, post-cutoff facts "fabrication" because
it answered from stale training memory. These helpers feed it today's date and
its cutoff so it knows where its knowledge ends and must search instead.
"""

import re
from unittest.mock import patch

from chat.agent import (
    MODEL_KNOWLEDGE_CUTOFF,
    create_fact_check_agent,
    temporal_grounding_prompt,
    today_str,
)


class TestTodayStr:
    def test_is_day_granular(self):
        """The date is day-level (e.g. "30 June 2026"), not a finer timestamp.

        Day granularity keeps the dynamic-system-prompt prefix stable within a
        day so vLLM's KV cache is only evicted once per day, not per request.
        """
        assert re.fullmatch(r"\d{1,2} [A-Z][a-z]+ \d{4}", today_str())

    def test_no_clock_time(self):
        """No hours/minutes/seconds leak in (those would thrash the KV cache)."""
        assert ":" not in today_str()


class TestTemporalGroundingPrompt:
    def test_includes_today_and_cutoff(self):
        prompt = temporal_grounding_prompt()
        assert today_str() in prompt
        assert MODEL_KNOWLEDGE_CUTOFF in prompt

    def test_instructs_to_search_not_trust_memory(self):
        """The fragment must push the model off stale memory toward searching."""
        prompt = temporal_grounding_prompt().lower()
        assert "cutoff" in prompt
        assert "search" in prompt
        # The exact failure we are fixing: do not call something fake on sight.
        assert "fabricated" in prompt or "false" in prompt


class TestAgentConstruction:
    def test_fact_check_agent_constructs_with_dynamic_grounding(self):
        """create_fact_check_agent registers the dynamic system prompt cleanly.

        The bot tests patch the fact-check agent, so this is the only coverage
        that the ``@fact_agent.system_prompt`` decorator API is actually valid;
        without it a pydantic-ai API mismatch would only surface at pod start.
        """
        with patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"):
            agent = create_fact_check_agent(base_url="http://fake:8080")
        assert agent is not None
