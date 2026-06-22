"""Unit test for scheduler.argo_handled - the ARGO_JOBS env parse that lets
on_startup_jobs skip in-process jobs an active Argo CronWorkflow now owns."""

from __future__ import annotations

import scheduler.api as api


def test_argo_handled_parses_env(monkeypatch):
    monkeypatch.delenv("ARGO_JOBS", raising=False)
    assert api.argo_handled("worldcup.refresh") is False

    # Comma-separated, tolerant of the trailing comma the chart emits.
    monkeypatch.setenv("ARGO_JOBS", "worldcup.refresh,knowledge.layout,")
    assert api.argo_handled("worldcup.refresh") is True
    assert api.argo_handled("knowledge.layout") is True
    assert api.argo_handled("other.job") is False

    monkeypatch.setenv("ARGO_JOBS", "")
    assert api.argo_handled("worldcup.refresh") is False
