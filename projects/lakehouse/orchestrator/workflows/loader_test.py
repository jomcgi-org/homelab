"""Tests for the workflow auto-discovery loader (hermetic — no Temporal)."""

from __future__ import annotations

import pytest

from projects.lakehouse.orchestrator import workflows as wf


@pytest.mark.parametrize("fn", [wf.discover_workflows, wf.discover_activities])
def test_discovery_returns_list(fn) -> None:
    # Always a list — empty at scaffold time, populated as workflow modules
    # (each exporting WORKFLOWS/ACTIVITIES) are added by later units.
    assert isinstance(fn(), list)


def test_entrypoint_module_excluded_from_discovery() -> None:
    # The 'run' entrypoint must never be treated as a workflow module.
    assert "run" in wf._NON_WORKFLOW_MODULES
