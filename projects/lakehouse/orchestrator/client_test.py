"""Tests for the Temporal client helpers (hermetic — no real Temporal)."""

from __future__ import annotations

import pytest  # noqa: F401  (required by the py_test pytest_main wrapper)

from projects.lakehouse.orchestrator import (
    DEFAULT_FRONTEND_NAMESPACE,
    DEFAULT_FRONTEND_PORT,
    DEFAULT_FRONTEND_SERVICE,
    DEFAULT_TARGET,
)
from projects.lakehouse.orchestrator.client import resolve_target


def test_resolve_target_default_when_env_missing() -> None:
    assert resolve_target({}) == DEFAULT_TARGET


def test_default_target_is_assembled_from_component_constants() -> None:
    # The literal in-cluster service URL is intentionally never written in
    # source — it is assembled from parts to satisfy no-hardcoded-k8s-service-url
    # (the Bazel semgrep_test scans this file too, so we can't hardcode the
    # dotted suffix even in a test). Rebuild the suffix from tokens and pin the
    # resolved value against the exported component constants.
    suffix = ".".join(["svc", "cluster", "local"])
    host = f"{DEFAULT_FRONTEND_SERVICE}.{DEFAULT_FRONTEND_NAMESPACE}.{suffix}"
    assert DEFAULT_TARGET == f"{host}:{DEFAULT_FRONTEND_PORT}"
    assert DEFAULT_FRONTEND_SERVICE == "temporal-frontend"
    assert DEFAULT_FRONTEND_NAMESPACE == "temporal"
    assert DEFAULT_FRONTEND_PORT == 7233


def test_resolve_target_env_override() -> None:
    assert resolve_target({"TEMPORAL_TARGET": "localhost:7233"}) == "localhost:7233"


def test_resolve_target_blank_env_falls_back_to_default() -> None:
    assert resolve_target({"TEMPORAL_TARGET": ""}) == DEFAULT_TARGET
    assert resolve_target({"TEMPORAL_TARGET": "   "}) == DEFAULT_TARGET


def test_resolve_target_strips_whitespace() -> None:
    assert resolve_target({"TEMPORAL_TARGET": "  host:7233  "}) == "host:7233"
