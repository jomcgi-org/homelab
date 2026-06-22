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


def test_register_job_skips_argo_handled(monkeypatch):
    """register_job must skip jobs an Argo CronWorkflow owns, so modules whose
    on_startup_jobs does not gate on argo_handled still avoid double-running."""

    async def _noop(_session):
        return None

    # Not handled -> registered.
    monkeypatch.setenv("ARGO_JOBS", "")
    api._registry.pop("test.job", None)
    api.register_job(_FakeSession(), name="test.job", interval_secs=60, handler=_noop)
    assert api.is_registered("test.job")

    # Handled -> skipped (and a stale registration is not re-added).
    api._registry.pop("test.job", None)
    monkeypatch.setenv("ARGO_JOBS", "test.job,")
    api.register_job(_FakeSession(), name="test.job", interval_secs=60, handler=_noop)
    assert not api.is_registered("test.job")


class _FakeSession:
    """Minimal stand-in: register_job only touches the session after the
    argo-handled skip, so the skip path never calls these, and the
    not-handled path exercises get/add/commit."""

    def get(self, *_a, **_k):
        return None

    def add(self, *_a, **_k):
        return None

    def commit(self):
        return None
