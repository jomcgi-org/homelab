"""Hermetic tests for ``tag_rotation`` (no Temporal server, no network).

Exercises the ``rotate_tags`` activity with a mocked boto3 S3 client, asserting
the building->current->previous->stale state-machine rotation and the
keep-last-N retention sweep; plus the pure ``_version_of`` / keep-last-N helper
and the workflow-defn registration. The 5-min grace ``workflow.sleep`` is a
workflow-body concern (not unit-tested without a server) — we assert the timer
constant instead.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import temporalio.workflow

from projects.lakehouse.orchestrator.workflows import tag_rotation as mod


# --------------------------------------------------------------------------- #
# Definition / registration / constants
# --------------------------------------------------------------------------- #


def test_workflow_is_defn_and_exported() -> None:
    assert mod.TagRotationWorkflow in mod.WORKFLOWS
    defn = temporalio.workflow._Definition.from_class(mod.TagRotationWorkflow)
    assert defn is not None


def test_activity_exported() -> None:
    assert mod.rotate_tags in mod.ACTIVITIES
    assert hasattr(mod.rotate_tags, "__temporal_activity_definition")


def test_grace_period_is_five_minutes() -> None:
    # platform/004 §lifecycle: 5-minute grace before demoting old current.
    assert mod.GRACE_PERIOD == timedelta(minutes=5)


def test_keep_last_n_is_24() -> None:
    assert mod.KEEP_LAST_N == 24


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_version_of_parses_filename() -> None:
    assert mod._version_of("serving/notes-v42.duckdb") == 42
    assert mod._version_of("serving/notes-v0.duckdb") == 0
    # Non-matching keys sort oldest.
    assert mod._version_of("serving/garbage.txt") == -1


def test_stale_beyond_keep_last_n_keeps_newest() -> None:
    keys = [f"serving/notes-v{n}.duckdb" for n in range(1, 6)]  # v1..v5
    # Keep 2 newest (v5, v4); stale the rest (v3, v2, v1).
    stale = mod._stale_beyond_keep_last_n(keys, keep=2)
    assert stale == [
        "serving/notes-v3.duckdb",
        "serving/notes-v2.duckdb",
        "serving/notes-v1.duckdb",
    ]


def test_stale_beyond_keep_last_n_keeps_all_when_under_limit() -> None:
    keys = [f"serving/notes-v{n}.duckdb" for n in range(1, 4)]
    assert mod._stale_beyond_keep_last_n(keys, keep=24) == []


# --------------------------------------------------------------------------- #
# rotate_tags activity — state machine + retention
# --------------------------------------------------------------------------- #


def _list_response(keys: list[str]) -> dict:
    return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


class _FakeS3:
    """In-memory fake S3 supporting list / get_object_tagging / put_object_tagging."""

    def __init__(self, states: dict[str, str]):
        # key -> state tag value
        self.states = dict(states)
        self.tag_writes: list[tuple[str, str]] = []

    def list_objects_v2(self, **kwargs):
        return _list_response(list(self.states.keys()))

    def get_object_tagging(self, *, Bucket, Key):
        state = self.states.get(Key)
        tagset = [{"Key": "state", "Value": state}] if state is not None else []
        return {"TagSet": tagset}

    def put_object_tagging(self, *, Bucket, Key, Tagging):
        value = Tagging["TagSet"][0]["Value"]
        self.states[Key] = value
        self.tag_writes.append((Key, value))


@pytest.mark.asyncio
async def test_rotate_tags_full_state_machine() -> None:
    # Existing: v1=current, v2=previous, v3=stale; new build v4=building.
    s3 = _FakeS3(
        {
            "serving/notes-v1.duckdb": "current",
            "serving/notes-v2.duckdb": "previous",
            "serving/notes-v3.duckdb": "stale",
            "serving/notes-v4.duckdb": "building",
        }
    )
    with patch.object(mod, "_s3_client", return_value=s3):
        result = await mod.rotate_tags("serving/notes-v4.duckdb")

    # Demotions: current->previous, previous->stale; stale stays stale.
    assert result.demoted["serving/notes-v1.duckdb"] == "previous"
    assert result.demoted["serving/notes-v2.duckdb"] == "stale"
    assert "serving/notes-v3.duckdb" not in result.demoted  # already stale
    # Promotion of the just-built artifact.
    assert result.promoted == "serving/notes-v4.duckdb"
    assert s3.states["serving/notes-v4.duckdb"] == "current"
    # Never two currents at once.
    currents = [k for k, v in s3.states.items() if v == "current"]
    assert currents == ["serving/notes-v4.duckdb"]


@pytest.mark.asyncio
async def test_rotate_tags_keep_last_n_sweeps_excess() -> None:
    # 26 building artifacts; keep 24, the 2 oldest get force-stale'd.
    states = {f"serving/notes-v{n}.duckdb": "previous" for n in range(1, 27)}
    states["serving/notes-v26.duckdb"] = "building"  # newest is the promote target
    s3 = _FakeS3(states)

    with patch.object(mod, "_s3_client", return_value=s3):
        result = await mod.rotate_tags("serving/notes-v26.duckdb")

    # v1 and v2 (oldest two) cleaned to stale by the retention sweep.
    assert set(result.cleaned) == {
        "serving/notes-v1.duckdb",
        "serving/notes-v2.duckdb",
    }
    assert s3.states["serving/notes-v1.duckdb"] == "stale"
    assert s3.states["serving/notes-v2.duckdb"] == "stale"
    # The promoted artifact is never stale'd by the sweep.
    assert s3.states["serving/notes-v26.duckdb"] == "current"


@pytest.mark.asyncio
async def test_rotate_tags_promote_only_no_others() -> None:
    s3 = _FakeS3({"serving/notes-v1.duckdb": "building"})
    with patch.object(mod, "_s3_client", return_value=s3):
        result = await mod.rotate_tags("serving/notes-v1.duckdb")
    assert result.promoted == "serving/notes-v1.duckdb"
    assert result.demoted == {}
    assert result.cleaned == []
    assert s3.states["serving/notes-v1.duckdb"] == "current"
