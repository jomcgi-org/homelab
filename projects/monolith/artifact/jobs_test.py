"""Tests for artifact.jobs (ADR 026 Phase 2 Task 2.5: session eviction handler).

Monkeypatches artifact.s3.prune_sessions so no S3 connection is needed.
"""

from __future__ import annotations

from artifact import jobs
from artifact import s3


def test_evict_stale_sessions_handler_returns_count_from_prune(monkeypatch):
    """Handler delegates to prune_sessions and returns the deleted count."""
    calls = []

    def fake_prune(max_age_days):
        calls.append(max_age_days)
        return 3

    monkeypatch.setattr(s3, "prune_sessions", fake_prune)

    result = jobs.evict_stale_sessions_handler()

    assert result == 3
    assert calls == [jobs.SESSION_TTL_DAYS]


def test_evict_stale_sessions_handler_passes_ttl_days(monkeypatch):
    """The TTL passed to prune_sessions equals SESSION_TTL_DAYS (default 30)."""
    captured = {}

    def fake_prune(max_age_days):
        captured["max_age_days"] = max_age_days
        return 0

    monkeypatch.setattr(s3, "prune_sessions", fake_prune)
    jobs.evict_stale_sessions_handler()

    assert captured["max_age_days"] == jobs.SESSION_TTL_DAYS
    assert jobs.SESSION_TTL_DAYS == 30  # default when env var absent


def test_evict_stale_sessions_handler_respects_env_override(monkeypatch):
    """ARTIFACT_SESSION_TTL_DAYS env var sets SESSION_TTL_DAYS at import time;
    the handler uses whatever the module constant resolves to."""
    captured = {}

    def fake_prune(max_age_days):
        captured["max_age_days"] = max_age_days
        return 7

    monkeypatch.setattr(s3, "prune_sessions", fake_prune)
    # Directly override the module constant for this test (env override happens
    # at module import time, so we patch the already-resolved attribute).
    monkeypatch.setattr(jobs, "SESSION_TTL_DAYS", 7)

    result = jobs.evict_stale_sessions_handler()

    assert result == 7
    assert captured["max_age_days"] == 7
