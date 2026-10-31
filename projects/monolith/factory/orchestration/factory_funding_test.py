"""The funding decision resolves one tranche without mutating its evidence."""

from copy import deepcopy

import pytest

from factory.orchestration import factory_funding as funding


def decision(**changes):
    return {
        "action": "continue",
        "reason": "The independent review identified a bounded correction.",
        "next_plan": "Correct the finding and request independent review.",
        "additional_work_turns": 2,
        "lease_minutes": 10,
        "task_budget_usd": 50,
        "assessment": {"progress": "PR", "proximity": "One finding", "value": "Bug"},
        **changes,
    }


def resolve(value, *, spent=4, budget=50, merit=None, deficit=None):
    inputs = {
        "spent": {"turns_used": spent},
        "policy": {"task_budget_usd": budget},
        "merit": merit,
        "deficit": deficit,
    }
    before = deepcopy((value, inputs))
    result = funding.decide(value, **inputs)
    assert (value, inputs) == before
    assert result["action"] in {"continue", "steer", "stop"}
    return result


@pytest.mark.parametrize("action", ["continue", "steer", "stop"])
@pytest.mark.parametrize("proposal", [0, 25, 50])
def test_action_preserves_the_approved_budget(action, proposal):
    result = resolve(decision(action=action, task_budget_usd=proposal))
    assert result == {
        "action": action,
        "work_turns": 0 if action == "stop" else 2,
        "task_budget_usd": 50,
        "escalation": None,
        "budget_approval": None,
    }


@pytest.mark.parametrize("needed,turns", [(None, 2), (True, 2), (3, 2), (9, 5)])
def test_recorded_turn_deficit_sets_the_minimum_tranche(needed, turns):
    result = resolve(decision(), deficit={"turns": {"needed": needed}})
    assert result["work_turns"] == turns


@pytest.mark.parametrize("ceiling,turns", [(9, 5), (7, 3)])
def test_merit_ceiling_reduces_only_the_requested_work(ceiling, turns):
    result = resolve(
        decision(additional_work_turns=5),
        merit={"ceiling": {"turn_ceiling": ceiling}},
    )
    assert result["work_turns"] == turns
    assert result["escalation"] is None


@pytest.mark.parametrize("ceiling", [3, 4, 8])
def test_unfundable_turn_deficit_steers_without_authorizing_work(ceiling):
    result = resolve(
        decision(),
        deficit={"turns": {"needed": 9}},
        merit={"ceiling": {"turn_ceiling": ceiling}},
    )
    assert result["action"] == "steer"
    assert result["work_turns"] == 0
    assert f"({ceiling} work turns)" in result["escalation"]


@pytest.mark.parametrize(
    "proposal,deficit", [(75, None), (50, {"usd": {"needed": 75}})]
)
def test_budget_proposals_retain_the_explicit_approval_boundary(proposal, deficit):
    result = resolve(
        decision(task_budget_usd=proposal),
        deficit=deficit,
        merit={"ceiling": {"turn_ceiling": 20}},
    )
    assert result["action"] == "steer"
    assert result["work_turns"] == 0
    assert result["task_budget_usd"] == 50
    assert result["budget_approval"] == {
        "approved_task_budget_usd": 50,
        "proposed_task_budget_usd": 75,
    }


def test_merit_doubt_is_a_steer_to_the_operator_without_a_grant():
    result = resolve(
        decision(action="escalate"), merit={"ceiling": {"turn_ceiling": 20}}
    )
    assert result["action"] == "steer"
    assert result["work_turns"] == 0
    assert result["escalation"].startswith("The funding judge doubts this task:")


@pytest.mark.parametrize(
    "changes,kwargs,reason",
    [
        ({"action": "escalate"}, {}, "escalate is honoured only"),
        ({"action": "approve"}, {}, "invalid funding action"),
        ({"additional_work_turns": 0}, {}, "finite work and lease"),
        ({"lease_minutes": 0}, {}, "finite work and lease"),
        ({"additional_work_turns": 21}, {}, "exceeds work turn bound"),
        ({}, {"deficit": {"turns": {"needed": 25}}}, "exceeds work turn bound"),
        ({"task_budget_usd": float("inf")}, {}, "budget is not finite"),
        ({"task_budget_usd": float("nan")}, {}, "budget is not finite"),
        (
            {"assessment": None},
            {"merit": {"ceiling": {"turn_ceiling": 20}}},
            "merit verdict needs its assessment",
        ),
    ],
)
def test_invalid_decisions_never_authorize_a_tranche(changes, kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        resolve(decision(**changes), **kwargs)


@pytest.mark.parametrize(
    "verdict,failures,runs,refusal",
    [
        ("changes_requested", 0, [], None),
        ("approve", 1, [], None),
        ("changes_requested", 1, [], "progress_repeated_review_failure"),
        ("approve", 0, [{"id": 11, "status": "failed"}], "progress_execution_failed"),
        (
            "approve",
            0,
            [{"id": 11, "status": "escalated"}],
            "progress_execution_failed",
        ),
        ("approve", 0, [{"id": 9, "status": "failed"}], None),
    ],
)
def test_operator_continuation_uses_the_same_decision(verdict, failures, runs, refusal):
    args = {
        "spent": {"turns_used": 8},
        "policy": {"task_budget_usd": 50},
        "continuation": {
            "previous_lease": {
                "review_id": 10,
                "verdict": "changes_requested",
                "consecutive_failures": failures,
            },
            "verdict": verdict,
            "runs": runs,
        },
    }
    if refusal:
        with pytest.raises(ValueError, match=refusal):
            funding.decide(decision(), **args)
    else:
        resolved = funding.decide(decision(), **args)
        assert resolved["action"] == "continue"
        assert resolved["work_turns"] == 2
        assert resolved["task_budget_usd"] == 50
