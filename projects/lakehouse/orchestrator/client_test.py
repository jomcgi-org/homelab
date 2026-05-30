"""Tests for the Temporal client helpers (hermetic — no real Temporal)."""

from __future__ import annotations

from projects.lakehouse.orchestrator import DEFAULT_TARGET
from projects.lakehouse.orchestrator.client import resolve_target


def test_resolve_target_default_when_env_missing() -> None:
    assert resolve_target({}) == DEFAULT_TARGET
    assert DEFAULT_TARGET == "temporal-frontend.temporal.svc.cluster.local:7233"


def test_resolve_target_env_override() -> None:
    assert resolve_target({"TEMPORAL_TARGET": "localhost:7233"}) == "localhost:7233"


def test_resolve_target_blank_env_falls_back_to_default() -> None:
    assert resolve_target({"TEMPORAL_TARGET": ""}) == DEFAULT_TARGET
    assert resolve_target({"TEMPORAL_TARGET": "   "}) == DEFAULT_TARGET


def test_resolve_target_strips_whitespace() -> None:
    assert resolve_target({"TEMPORAL_TARGET": "  host:7233  "}) == "host:7233"
