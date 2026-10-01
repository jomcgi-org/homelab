from __future__ import annotations

from collections import Counter
from dataclasses import replace
import time

import pytest
from dbos import DBOS
from dbos._dbos import _get_or_create_dbos_registry
from dbos._error import DBOSUnexpectedStepError
from dbos._utils import GlobalParams

from factory.execution import provider_quota
from factory.orchestration import drainer, node_workflows, runtime, steps  # noqa: F401 - register before snapshot


_STEP_LIST = "old"


def _result(handle):
    deadline = time.monotonic() + 10
    while handle.get_status().status in {"PENDING", "ENQUEUED"}:
        assert time.monotonic() < deadline, "DBOS test workflow did not finish"
        time.sleep(0.01)
    return handle.get_result(polling_interval_sec=0.01)


@pytest.fixture
def real_dbos(tmp_path, monkeypatch):
    """Launch the production config on SQLite, then stop DBOS and restore globals."""
    registry = _get_or_create_dbos_registry()
    workflows = dict(registry.workflow_info_map)
    types = dict(registry.function_type_map)
    version = GlobalParams.app_version
    executor_id = GlobalParams.executor_id
    monkeypatch.setitem(globals(), "_STEP_LIST", "old")
    effects = Counter()

    @DBOS.step()
    def first():
        effects["first"] += 1
        return effects["first"]

    @DBOS.step()
    def removable():
        effects["removable"] += 1
        return effects["removable"]

    @DBOS.step()
    def added():
        effects["added"] += 1

    @DBOS.step()
    def last():
        effects["last"] += 1
        return effects["last"]

    @DBOS.workflow()
    def long_lived():
        if _STEP_LIST == "added":
            if DBOS.patch("replay-test-add-before-first"):
                added()
        elif _STEP_LIST == "ungated":
            added()
        outputs = [first()]
        if _STEP_LIST != "removed" or not DBOS.patch("replay-test-remove-middle"):
            outputs.append(removable())
        outputs.append(last())
        return outputs

    try:
        DBOS(config=runtime.build_dbos_config(f"sqlite:///{tmp_path / 'dbos.sqlite'}"))
        DBOS.launch()
        assert GlobalParams.app_version == runtime.node_workflow_version()
        yield long_lived, effects
    finally:
        DBOS.destroy()
        registry.workflow_info_map.clear()
        registry.workflow_info_map.update(workflows)
        registry.function_type_map.clear()
        registry.function_type_map.update(types)
        GlobalParams.app_version = version
        GlobalParams.executor_id = executor_id


@pytest.mark.parametrize("new_list", ["added", "removed"])
def test_patch_replays_old_checkpoints_and_runs_new_behavior_only_when_fresh(
    real_dbos, new_list
):
    global _STEP_LIST
    workflow, effects = real_dbos
    old = DBOS.start_workflow(workflow)
    assert _result(old) == [1, 1, 1]
    assert old.get_status().status == "SUCCESS"
    assert [
        step["function_id"] for step in DBOS.list_workflow_steps(old.workflow_id)
    ] == [
        1,
        2,
        3,
    ]

    _STEP_LIST = new_list
    # Copy both pre-patch checkpoints, then replay them against the changed body.
    replay = DBOS.fork_workflow(old.workflow_id, start_step=3)
    assert _result(replay) == [1, 1, 2]
    assert replay.get_status().status == "SUCCESS"
    assert effects == Counter(first=1, removable=1, last=2)

    fresh = DBOS.start_workflow(workflow)
    if new_list == "added":
        assert _result(fresh) == [2, 2, 3]
        assert effects == Counter(first=2, removable=2, last=3, added=1)
        # Patched history must also replay its marker and recorded added step.
        patched_replay = DBOS.fork_workflow(fresh.workflow_id, start_step=5)
        assert _result(patched_replay) == [2, 2, 4]
        assert effects == Counter(first=2, removable=2, last=4, added=1)
    else:
        assert _result(fresh) == [2, 3]
        assert effects == Counter(first=2, removable=1, last=3)
        patched_replay = DBOS.fork_workflow(fresh.workflow_id, start_step=3)
        assert _result(patched_replay) == [2, 4]
        assert effects == Counter(first=2, removable=1, last=4)
    assert fresh.get_status().status == "SUCCESS"
    assert patched_replay.get_status().status == "SUCCESS"


def test_ungated_addition_reproduces_unexpected_step_error(real_dbos):
    global _STEP_LIST
    workflow, effects = real_dbos
    old = DBOS.start_workflow(workflow)
    assert _result(old) == [1, 1, 1]
    _STEP_LIST = "ungated"
    replay = DBOS.fork_workflow(old.workflow_id, start_step=3)
    with pytest.raises(DBOSUnexpectedStepError):
        _result(replay)
    assert replay.get_status().status == "ERROR"
    assert effects == Counter(first=1, removable=1, last=1)


def test_real_disabled_drain_cycle_preserves_checkpoint_baseline(
    real_dbos, monkeypatch
):
    calls = Counter()
    settings = replace(drainer.agent_config.load_drainer_settings(), enabled=False)

    def fetch_quota():
        calls["quota"] += 1
        return {"available": False}

    def load_settings():
        calls["settings"] += 1
        return settings

    def forbid_database():
        raise AssertionError("replay test must not access the application database")

    monkeypatch.setattr(provider_quota, "fetch_provider_quota_sync", fetch_quota)
    monkeypatch.setattr(drainer.agent_config, "load_drainer_settings", load_settings)
    monkeypatch.setattr("core.db.get_engine", forbid_database)
    old = DBOS.start_workflow(drainer.drain_cycle)
    assert _result(old) == {"status": "disabled", "processed": 0}
    assert old.get_status().status == "SUCCESS"
    baseline = ["_quota_span_attributes", "pin_drainer_settings"]
    assert [
        step["function_name"] for step in DBOS.list_workflow_steps(old.workflow_id)
    ] == baseline, "FACTORY.md: gate checkpoint sequence changes with DBOS.patch"

    replay = DBOS.fork_workflow(old.workflow_id, start_step=2)
    assert _result(replay) == {"status": "disabled", "processed": 0}
    assert replay.get_status().status == "SUCCESS"
    assert [
        step["function_name"] for step in DBOS.list_workflow_steps(replay.workflow_id)
    ] == baseline, "FACTORY.md: gate checkpoint sequence changes with DBOS.patch"
    assert calls == Counter(quota=1, settings=2)
