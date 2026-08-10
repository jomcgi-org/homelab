from datetime import datetime, timezone

from swarm.view import compose_master, compose_run


class Status:
    def __init__(self, workflow_id, status, args, output=None, **extra):
        self.workflow_id = workflow_id
        self.status = status
        self.input = {"args": args}
        self.output = output
        self.app_version = "unknown"
        self.created_at = datetime(2026, 8, 10, 4, 6, 45, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 8, 10, 4, 10, 58, tzinfo=timezone.utc)
        for key, value in extra.items():
            setattr(self, key, value)


class Workflow:
    def __init__(self, status, attributes=None):
        self.status = status
        self.attributes = attributes or {}

    def get_status(self):
        return self.status


class DBOS:
    def __init__(self, workflow, steps=None, queued=None):
        self.workflow = workflow
        self.steps = steps or []
        self.queued = queued or []

    def retrieve_workflow(self, workflow_id):
        return (
            self.workflow if self.workflow.status.workflow_id == workflow_id else None
        )

    def list_workflow_steps(self, workflow_id):
        return self.steps

    def list_workflows(self, **kwargs):
        return []

    def list_queued_workflows(self, **kwargs):
        return self.queued


def session(n, status="warn", node="implement", attempt=1):
    return {
        "id": 171,
        "local_session_id": f"swarm-smoke-2-{node}-{attempt}",
        "node_key": node,
        "node_attempt": attempt,
        "status": status,
        "model": "luna",
        "total_cost_usd": 0,
        "created_at": datetime(2026, 8, 10, 4, 6, 57, tzinfo=timezone.utc),
        "last_turn_at": datetime(2026, 8, 10, 4, 7, 35, tzinfo=timezone.utc),
    }


# Copied from contract v4 fixture fx4, the expected run fragments under test.
FX4_EXPECTED_PLAN = {
    "pinned": True,
    "source": "recorded",
    "max_attempts": 2,
    "implementer_model": "luna",
    "reviewer_model": "opus",
    "turn_timeout_seconds": 1800,
    "version": 1,
    "budget_usd": None,
}
FX4_EXPECTED_CANCELLED_BY = {"actor": "joe", "at": "2026-08-10T04:10:58Z"}


def test_cancelled_run_uses_engine_state_and_preserves_warn_session():
    status = Status(
        "swarm-smoke-2",
        "CANCELLED",
        [
            "Add a docstring to next_action and push the branch",
            "jomcgi/homelab",
            "main",
        ],
    )
    run = compose_run(
        DBOS(
            Workflow(
                status,
                {"plan": FX4_EXPECTED_PLAN, "cancelled_by": FX4_EXPECTED_CANCELLED_BY},
            )
        ),
        "swarm-smoke-2",
        [session(171)],
        "b7a44d1c",
    )
    assert run["state"] == "cancelled"
    assert run["dbos_status"] == "CANCELLED"
    assert run["cancelled_by"] == FX4_EXPECTED_CANCELLED_BY
    assert run["nodes"][0]["attempts"][0]["state"] == "failed"


def test_escalated_success_carries_branch_head_and_no_commit():
    status = Status(
        "wf-1",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {
            "status": "escalated",
            "branch_head": "e51a09c2",
            "work_branch": "claude/wf-1",
        },
    )
    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})), "wf-1", [], "b7a44d1c"
    )
    assert run["state"] == "escalated"
    assert run["nodes"][1]["evidence"]["kind"] == "branch_head"
    assert run["nodes"][2]["verdict"] is None


def test_stranded_pending_has_no_decision():
    status = Status("wf-2", "PENDING", ["task", "repo", "main"], app_version="old")
    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})), "wf-2", [], "new"
    )
    assert run["state"] == "stranded"
    assert run["stranded"] is True
    assert run["app_version"] == "old"
    assert run["server_app_version"] == "new"
    assert run["nodes"][1]["decision"] is None


def _running_implement(workflow_id):
    """One in-flight implement attempt.

    A decision renders on the ACTIVE gate only. A run with no sessions at all
    has nothing running, so its gate is still in the future and correctly
    carries no decision; the register distinction only exists once the gate is
    actually being evaluated.
    """
    return [
        {
            "id": 1,
            "local_session_id": f"{workflow_id}-implement-1",
            "node_key": "implement",
            "node_attempt": 1,
            "status": "running",
            "model": "luna",
            "created_at": datetime(2026, 8, 10, 22, 50, tzinfo=timezone.utc),
            "last_turn_at": datetime(2026, 8, 10, 22, 56, tzinfo=timezone.utc),
            "total_cost_usd": 0.09,
        }
    ]


def test_pinned_and_unpinned_registers_are_distinct(monkeypatch):
    pinned_status = Status("wf-3", "PENDING", ["task", "repo", "main"])
    pinned = compose_run(
        DBOS(Workflow(pinned_status, {"plan": FX4_EXPECTED_PLAN})),
        "wf-3",
        _running_implement("wf-3"),
        "unknown",
    )
    assert pinned["plan"] == FX4_EXPECTED_PLAN
    assert pinned["nodes"][1]["decision"]["register"] == "fact"

    monkeypatch.setenv("SWARM_IMPLEMENTER_MODEL", "luna")
    unpinned_status = Status("wf-4", "PENDING", ["task", "repo", "main"])
    unpinned = compose_run(
        DBOS(Workflow(unpinned_status)), "wf-4", _running_implement("wf-4"), "unknown"
    )
    assert unpinned["plan"]["pinned"] is False
    assert unpinned["plan"]["max_attempts"] is None
    assert unpinned["nodes"][1]["decision"]["register"] == "belief"


def test_queued_child_gets_codex_position():
    status = Status("wf-5", "PENDING", ["task", "repo", "main"])
    run = compose_run(
        DBOS(
            Workflow(status, {"plan": FX4_EXPECTED_PLAN}),
            queued=[{"workflow_id": "wf-5"}],
        ),
        "wf-5",
        [],
        "unknown",
    )
    assert run["state"] == "queued"
    assert run["nodes"][0]["queue"] == {"name": "codex", "position": 1}


def test_shas_render_at_eight_characters():
    # The contract fixtures are explicit that every sha on this surface is
    # short, and the server owns every string the client shows. A full 40
    # character sha in a nowrap meta line ellipsizes into nothing useful, so
    # this pins the wording the mockup was approved against.
    full = "86dcbf416158031d18c65a3c37120e044dc13fbc"
    status = Status(
        "wf-sha",
        "SUCCESS",
        ["task", "repo", "main"],
        output={"status": "review", "review_verdict": "approve", "branch_head": full},
    )
    run = compose_run(DBOS(Workflow(status)), "wf-sha", [], "unknown")
    gate = next(n for n in run["nodes"] if n["key"] == "push_gate")
    assert gate["evidence"]["summary"] == (
        "head 86dcbf41 observed on the remote after the turn"
    )


def test_attempt_carries_prior_head_alongside_finding():
    status = Status("wf-prior", "SUCCESS", ["task", "repo", "main"])
    run = compose_run(
        DBOS(
            Workflow(status),
            steps=[
                {"function_name": "read_branch_head", "output": "1234567890"},
                {"function_name": "read_branch_head", "output": "abcdef0123"},
            ],
        ),
        "wf-prior",
        [session(1, status="completed")],
        "unknown",
    )
    attempt = run["nodes"][0]["attempts"][0]
    assert attempt["prior_head"] == "12345678"
    assert attempt["finding"]["observed_head"] == "abcdef01"


def test_master_rows_include_active_and_shape():
    status = Status("wf-master", "PENDING", ["task", "repo", "main"])

    class ListingDBOS(DBOS):
        def list_workflows(self, **kwargs):
            return [self.workflow]

    result = compose_master(
        ListingDBOS(Workflow(status)),
        True,
        {"wf-master": []},
        "unknown",
    )
    assert result["runs"][0]["active"] is True
    assert result["runs"][0]["shape"] == [
        {"key": "implement", "kind": "work", "state": "future"},
        {"key": "push_gate", "kind": "gate", "state": "future"},
        {"key": "review", "kind": "work", "state": "future"},
    ]
