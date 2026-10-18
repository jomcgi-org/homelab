"""Tests for the summary entrypoint used by the one-shot Argo job."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat import summarizer


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
async def test_summary_job_awaits_both_generators(injected):
    session = MagicMock()
    llm_call = AsyncMock()
    with (
        patch.object(summarizer, "build_llm_caller", return_value=llm_call) as build,
        patch.object(summarizer, "generate_summaries", new_callable=AsyncMock) as users,
        patch.object(
            summarizer, "generate_channel_summaries", new_callable=AsyncMock
        ) as channels,
    ):
        await summarizer.run_summary_generation(
            session, llm_call=llm_call if injected else None
        )

    if injected:
        build.assert_not_called()
    else:
        build.assert_called_once_with()
    users.assert_awaited_once_with(session, llm_call)
    channels.assert_awaited_once_with(session, llm_call)
