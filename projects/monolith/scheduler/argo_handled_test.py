"""ARGO_JOBS names accepted for run-now without a retained scheduler row."""

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


def test_argo_handled_ignores_whitespace_and_empty_entries(monkeypatch):
    monkeypatch.setenv("ARGO_JOBS", " worldcup.refresh , , knowledge.layout, \t")
    assert api.argo_handled("worldcup.refresh") is True
    assert api.argo_handled("knowledge.layout") is True
    assert api.argo_handled("") is False
    assert api.argo_handled("other.job") is False
