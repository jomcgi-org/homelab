"""Deployment compatibility for factory-owned durable execution."""

import hashlib
import importlib
import inspect
import pickle

import pytest

from factory.durable_identity import durable_source


def test_only_absolute_import_modules_are_normalized():
    source = """def step():
    # factory.execution.models must stay in this comment.
    text = "factory.orchestration.steps"
    from factory.execution.models import (
        AgentSession,
    )
    from factory.orchestration import steps
    from .execution import local
    return text
"""
    expected = source.replace(
        "from factory.execution.models", "from agent_sessions.models"
    ).replace("from factory.orchestration import", "from swarm import")
    assert durable_source(source) == expected
    assert durable_source(expected) == expected
    assert durable_source(source.replace("return text", "return None")) != expected


def test_indented_source_and_unicode_columns_are_preserved():
    source = (
        '    def step():\n        label = "é"; from factory.execution import models\n'
    )
    assert durable_source(source) == source.replace(
        "from factory.execution", "from agent_sessions"
    )


def test_funding_profile_intentionally_versions_node_workflow():
    from dbos._utils import GlobalParams

    from factory.orchestration.runtime import (
        _node_workflow_members,
        node_workflow_version,
    )

    sources = sorted(
        durable_source(inspect.getsource(f)) for f in _node_workflow_members()
    )
    # The package-only move retained 33891819c41b8a41725d7869a5059ed7.
    # #7583 intentionally changes _read_turn_artifact's durable result contract:
    # funding artifacts now require exact runtime profile metadata. Keep a new
    # version, never replay that check as the historical checkpoint body.
    # Derived after final formatting, which changed the unformatted
    # 0485521be38f2d12fcee143bf6d370f2 fingerprint. Do not normalize whitespace
    # or alter durable_source to make new code impersonate either prior version.
    source_hash = hashlib.md5("".join(sources).encode())
    assert source_hash.hexdigest() == "5e16dc2336c4950bcff0cad36bc39286"
    source_hash.update(GlobalParams.dbos_version.encode())
    assert node_workflow_version() == source_hash.hexdigest()


@pytest.mark.parametrize(
    "legacy,current,names",
    [
        (
            "agent_sessions.store",
            "factory.execution.store",
            ("SessionOutcomeUnknown", "PendingClaimLost"),
        ),
        (
            "agent_sessions.transport",
            "factory.execution.transport",
            (
                "EmberTurnNotInvoked",
                "EmberControlPlaneUnavailable",
                "EmberSessionGone",
            ),
        ),
        (
            "swarm.store",
            "factory.orchestration.store",
            ("NoOpenDecision", "InvalidDecision"),
        ),
        (
            "swarm.drainer",
            "factory.orchestration.drainer",
            ("MalformedPayload", "InvocationOutcomeUnknown"),
        ),
        (
            "swarm.factory_conductor",
            "factory.orchestration.factory_conductor",
            (
                "PlannerContextOverflow",
                "PlannerContextAuthorizationError",
            ),
        ),
    ],
)
def test_durable_errors_keep_one_class_and_the_legacy_pickle_path(
    legacy, current, names
):
    old_module = importlib.import_module(legacy)
    new_module = importlib.import_module(current)
    for name in names:
        cls = getattr(new_module, name)
        assert cls is getattr(old_module, name)
        assert cls.__module__ == legacy
        error = cls("recorded failure")
        encoded = pickle.dumps(error)
        assert legacy.encode() in encoded
        decoded = pickle.loads(encoded)
        assert type(decoded) is cls
        assert decoded.args == error.args


def test_discord_adapter_resolves_existing_thread_operations_only(monkeypatch):
    from agent_sessions import api as adapter
    from factory.execution import api

    marker = object()
    monkeypatch.setattr(api, "start_session_for_thread", marker)
    assert adapter.start_session_for_thread is marker
    with pytest.raises(AttributeError):
        adapter.start_session_for_swarm
