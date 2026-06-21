"""Routing tests for worldcup.refresh_dispatch.

Verifies the JOB_EXECUTOR gate: default runs refresh_handler in-process; "argo"
submits a Workflow instead. Both the handler and the submitter are patched, so
no network, DB, or cluster access happens.
"""

from __future__ import annotations

from unittest import mock

import pytest

import worldcup.jobs as jobs


@pytest.mark.asyncio
async def test_dispatch_runs_in_process_by_default(monkeypatch):
    submit = mock.AsyncMock()
    handler = mock.AsyncMock(return_value=None)
    monkeypatch.setattr("scheduler.api.jobs_use_argo", lambda: False)
    monkeypatch.setattr("scheduler.api.submit_job_workflow", submit)
    monkeypatch.setattr(jobs, "refresh_handler", handler)

    await jobs.refresh_dispatch("sess")

    handler.assert_awaited_once_with("sess")
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_submits_workflow_when_flagged(monkeypatch):
    submit = mock.AsyncMock(return_value="worldcup-sim-1")
    handler = mock.AsyncMock()
    monkeypatch.setattr("scheduler.api.jobs_use_argo", lambda: True)
    monkeypatch.setattr("scheduler.api.submit_job_workflow", submit)
    monkeypatch.setattr(jobs, "refresh_handler", handler)

    result = await jobs.refresh_dispatch("sess")

    assert result is None
    submit.assert_awaited_once()
    _, kwargs = submit.call_args
    assert kwargs["name"] == "worldcup-sim"
    assert kwargs["args"] == ["worldcup-sim"]
    handler.assert_not_called()
