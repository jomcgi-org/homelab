import re
from datetime import datetime, timezone

import pytest

from swarm.view import (
    _disposition,
    _structured_verdict,
    compose_master,
    compose_run,
)


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
    assert run["deviations"] == []


def test_compose_run_populates_blocked_on_from_open_decision():
    status = Status("wf-blocked", "PENDING", ["task", "repo", "main"])
    requested_at = datetime(2026, 8, 22, 12, 30, tzinfo=timezone.utc)

    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})),
        status.workflow_id,
        [],
        "unknown",
        [
            {
                "id": 42,
                "node_key": "push_gate",
                "kind": "push_gate",
                "options": ["approve", "send_back"],
                "note": "No branch movement was verified.",
                "requested_at": requested_at,
            }
        ],
    )

    push_gate = run["nodes"][1]
    assert push_gate["state"] == "blocked"
    assert push_gate["blocked_on"] == {
        "kind": "human",
        "note": "No branch movement was verified.",
        "since": "2026-08-22T12:30:00Z",
        "decision_id": 42,
        "options": ["approve", "send_back"],
        "decision_kind": "push_gate",
    }
    assert run["nodes"][0]["blocked_on"] is None
    assert run["nodes"][2]["blocked_on"] is None
    assert run["disposition"] == {
        "state": "gated",
        "reason": "No branch movement was verified.",
        "next": "choose one of: approve, send_back",
    }


@pytest.mark.parametrize("dbos_status", ["SUCCESS", "CANCELLED"])
def test_compose_run_ignores_open_decisions_for_terminal_workflow(dbos_status):
    status = Status("wf-terminal-decision", dbos_status, ["task", "repo", "main"])

    run = compose_run(
        DBOS(Workflow(status)),
        status.workflow_id,
        [],
        "unknown",
        [
            {
                "id": 45,
                "node_key": "push_gate",
                "kind": "push_gate",
                "options": ["approve", "send_back"],
                "note": None,
                "requested_at": datetime(2026, 8, 22, tzinfo=timezone.utc),
            }
        ],
    )

    assert all(node["blocked_on"] is None for node in run["nodes"])
    assert all(node["state"] != "blocked" for node in run["nodes"])


def test_compose_master_needs_fires_for_open_human_decision():
    status = Status("wf-needs", "PENDING", ["task", "repo", "main"])

    class ListingDBOS(DBOS):
        def list_workflows(self, **kwargs):
            if "parent_workflow_id" in kwargs:
                return []
            return [self.workflow]

    loaded = []

    def load_decisions(workflow_ids):
        loaded.append(workflow_ids)
        return {
            status.workflow_id: [
                {
                    "id": 43,
                    "node_key": "review",
                    "kind": "review_escalation",
                    "options": ["retry", "send_back"],
                    "note": "The retry did not move the branch.",
                    "requested_at": datetime(2026, 8, 22, tzinfo=timezone.utc),
                }
            ]
        }

    result = compose_master(
        ListingDBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})),
        True,
        {status.workflow_id: []},
        "unknown",
        decision_loader=load_decisions,
    )

    assert loaded == [[status.workflow_id]]
    assert result["runs"][0]["current"] == {"label": "review", "state": "blocked"}
    assert result["runs"][0]["needs"] == {
        "kind": "human",
        "reason": "waiting on your decision",
        "decision_kind": "review_escalation",
    }


@pytest.mark.parametrize("decision", ["send_back", "expired"])
def test_compose_run_surfaces_decision_record_on_owning_node(decision):
    decision_record = {
        "decision_id": 44,
        "node_key": "push_gate",
        "kind": "push_gate",
        "decision": decision,
        "ask": "No branch movement was verified.",
        "decision_note": "Do not ship this." if decision != "expired" else None,
        "actor_subject": "alice@example.com" if decision != "expired" else None,
        "actor_authority": "cloudflare-access" if decision != "expired" else None,
        "decided_at": "2026-08-22T12:35:00+00:00",
    }
    status = Status(
        "wf-decision-record",
        "SUCCESS",
        ["task", "repo", "main"],
        {"status": "escalated", "decision": decision_record},
    )

    run = compose_run(DBOS(Workflow(status)), status.workflow_id, [], "unknown")

    assert run["nodes"][1]["decision_record"] == decision_record
    assert run["nodes"][1]["decision"] is None
    assert run["nodes"][0]["decision_record"] is None
    assert run["nodes"][2]["decision_record"] is None


def test_disposition_names_recorded_decision_and_actor():
    disposition = _disposition(
        "SUCCESS",
        "escalated",
        {
            "decision": {
                "decision": "send_back",
                "actor_subject": "alice@example.com",
            }
        },
        [],
        {},
    )

    assert disposition["reason"] == "decided send_back by alice@example.com"


def test_disposition_describes_approval_that_still_escalated():
    disposition = _disposition(
        "SUCCESS",
        "escalated",
        {
            "decision": {
                "decision": "approve",
                "actor_subject": "alice@example.com",
            }
        },
        [],
        {},
    )

    assert disposition["reason"] == (
        "approved by alice@example.com, then escalated: "
        "the run was escalated, awaiting human review"
    )


def test_blocked_disposition_uses_label_when_decision_has_no_note():
    disposition = _disposition(
        "PENDING",
        "blocked",
        {},
        [
            {
                "label": "review",
                "blocked_on": {
                    "kind": "human",
                    "note": None,
                    "options": ["retry", "send_back"],
                },
            }
        ],
        {},
    )

    assert disposition == {
        "state": "gated",
        "reason": "review is waiting on your decision",
        "next": "choose one of: retry, send_back",
    }


def test_waiting_gate_keeps_policy_and_recorded_decisions():
    decided_at = datetime(2026, 8, 22, 12, 35, tzinfo=timezone.utc)
    status = Status(
        "wf-waiting-decision-record",
        "PENDING",
        ["task", "repo", "main"],
        {
            "decision": {
                "decision_id": 46,
                "node_key": "push_gate",
                "kind": "push_gate",
                "decision": "approve",
                "ask": "Approve the unverified branch?",
                "decision_note": None,
                "actor_subject": "alice@example.com",
                "actor_authority": "cloudflare-access",
                "decided_at": decided_at,
            }
        },
    )

    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})),
        status.workflow_id,
        _running_implement(status.workflow_id),
        "unknown",
    )

    gate = run["nodes"][1]
    assert gate["state"] == "waiting"
    assert gate["decision"]["basis"] == "policy.next_action"
    assert gate["decision_record"]["decision"] == "approve"
    assert gate["decision_record"]["decided_at"] == "2026-08-22T12:35:00Z"


def test_composed_run_attaches_mechanical_deviations():
    status = Status(
        "wf-deviations",
        "SUCCESS",
        ["task", "repo", "main"],
        {"status": "escalated"},
    )
    run = compose_run(
        DBOS(
            Workflow(status, {"plan": FX4_EXPECTED_PLAN}),
            steps=[
                {"function_name": "read_branch_head", "output": "same"},
                {"function_name": "read_branch_head", "output": "same"},
                {"function_name": "read_branch_head", "output": "same"},
                {"function_name": "read_branch_head", "output": "same"},
            ],
        ),
        "wf-deviations",
        [session(1), session(2, attempt=2)],
        "unknown",
    )
    assert [deviation["code"] for deviation in run["deviations"]] == [
        "attempts_exhausted",
        "retry_taken",
    ]


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


def test_unresolvable_server_version_does_not_strand():
    """An unknown server version is not evidence of stranding.

    Observed in production: the server version resolved to a placeholder no real
    DBOS version could equal, so every PENDING run rendered a "will not resume
    unaided" banner while its nodes were still running.
    """
    status = Status("wf-3", "PENDING", ["task", "repo", "main"], app_version="real")
    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})), "wf-3", [], ""
    )
    assert run["stranded"] is False
    assert run["state"] != "stranded"


def test_missing_run_version_does_not_strand():
    status = Status("wf-4", "PENDING", ["task", "repo", "main"], app_version="")
    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})), "wf-4", [], "new"
    )
    assert run["stranded"] is False
    assert run["state"] != "stranded"


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
            "final_result_text": (
                "RATIONALE\n- path: swarm/view.py · why: attaches testimony\n"
                "- deviation: no routing changes"
            ),
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


def test_attempt_carries_parsed_rationale_and_absence():
    parsed = compose_run(
        DBOS(Workflow(Status("wf-rationale", "PENDING", ["task", "repo", "main"]))),
        "wf-rationale",
        _running_implement("wf-rationale"),
        "unknown",
    )
    assert parsed["nodes"][0]["attempts"][0]["rationale"]["parse_status"] == "parsed"

    absent_row = _running_implement("wf-rationale")[0]
    absent_row["final_result_text"] = None
    absent = compose_run(
        DBOS(
            Workflow(Status("wf-rationale-none", "PENDING", ["task", "repo", "main"]))
        ),
        "wf-rationale-none",
        [absent_row],
        "unknown",
    )
    assert absent["nodes"][0]["attempts"][0]["rationale"]["parse_status"] == "none"


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


def test_request_changes_run_paints_completed_nodes_and_timestamps():
    status = Status(
        "wf-request-changes",
        "SUCCESS",
        ["task", "repo", "main"],
        output={
            "status": "review",
            "review_verdict": "request_changes",
        },
        created_at=1783725765000,
        updated_at=1783726258000,
        completed_at=1783726258000,
    )
    run = compose_run(
        DBOS(Workflow(status)),
        "wf-request-changes",
        [session(1, status="completed"), session(2, status="completed", node="review")],
        "unknown",
    )
    assert [node["state"] for node in run["nodes"]] == ["done", "passed", "done"]
    # 1783725765000 ms since the epoch, computed rather than eyeballed.
    assert run["created_at"] == "2026-07-10T23:22:45Z"
    assert run["updated_at"] == "2026-07-10T23:30:58Z"
    assert run["completed_at"] == "2026-07-10T23:30:58Z"


def test_compose_run_disposition_approved():
    status = Status(
        "wf-disposition-approved",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {"status": "review", "review_verdict": "approve"},
    )
    run = compose_run(
        DBOS(Workflow(status, {"plan": FX4_EXPECTED_PLAN})),
        status.workflow_id,
        [],
        "unknown",
    )
    assert run["disposition"] == {
        "state": "approved",
        "reason": "the reviewer approved the changes",
        "next": None,
    }


def test_compose_run_disposition_cycles_exhausted():
    plan = {**FX4_EXPECTED_PLAN, "max_review_cycles": 2}
    status = Status(
        "wf-disposition-exhausted",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {
            "status": "review_cycles_exhausted",
            "review_verdict": "request_changes",
            "review_text": "Please address this.",
        },
    )
    run = compose_run(
        DBOS(Workflow(status, {"plan": plan})), status.workflow_id, [], "unknown"
    )
    assert run["disposition"]["state"] == "review_cycles_exhausted"
    assert (
        run["disposition"]["reason"]
        == "the maximum number of review cycles was reached"
    )


def test_compose_run_disposition_unparseable():
    status = Status(
        "wf-disposition-unparseable",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {"status": "review", "review_verdict": "unparseable"},
    )
    run = compose_run(DBOS(Workflow(status)), status.workflow_id, [], "unknown")
    assert run["disposition"]["state"] == "escalated"


def test_structured_verdict_parses_approve():
    verdict = _structured_verdict(
        {"review_text": "Looks good.\nVERDICT: APPROVE", "commit_sha": "1234567890"},
        "jomcgi/homelab",
    )
    assert verdict["value"] == "approve"
    assert verdict["summary_plain"] == "approved the changes"
    assert verdict["commit_sha"] == "12345678"


def test_structured_verdict_handles_unparseable():
    verdict = _structured_verdict({"review_text": "No verdict here"}, "repo")
    assert verdict["value"] == "unparseable"
    assert verdict["summary_plain"] == "verdict could not be parsed"


def test_disposition_shows_changes_requested():
    disposition = _disposition("SUCCESS", "changes_requested", {}, [], {})
    assert disposition["state"] == "changes_requested"


def test_disposition_shows_review_cycles_exhausted():
    disposition = _disposition(
        "SUCCESS", "changes_requested", {"status": "review_cycles_exhausted"}, [], {}
    )
    assert disposition["state"] == "review_cycles_exhausted"


def test_compose_run_disposition_cancelled():
    status = Status("wf-disposition-cancelled", "CANCELLED", ["task", "repo", "main"])
    run = compose_run(
        DBOS(Workflow(status, {"cancelled_by": {"actor": "joe"}})),
        status.workflow_id,
        [],
        "unknown",
    )
    assert run["disposition"]["state"] == "cancelled"
    assert run["disposition"]["reason"] == "this run was cancelled"


def test_compose_run_verdict_structured():
    full_sha = "86dcbf416158031d18c65a3c37120e044dc13fbc"
    status = Status(
        "wf-verdict-structured",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {
            "status": "review",
            "review_verdict": "approve",
            "review_text": "The change is ready to merge.",
            "commit_sha": full_sha,
        },
    )
    run = compose_run(DBOS(Workflow(status)), status.workflow_id, [], "unknown")
    verdict = run["nodes"][2]["verdict"]
    assert verdict == {
        "value": "approve",
        "summary_plain": "approved the changes",
        "text_md": "The change is ready to merge.",
        "commit_sha": "86dcbf41",
        "commit_url": f"https://github.com/jomcgi/homelab/commit/{full_sha}",
    }


def test_compose_run_events_full_sequence():
    status = Status(
        "wf-events",
        "SUCCESS",
        ["task", "jomcgi/homelab", "main"],
        {
            "status": "review",
            "review_verdict": "approve",
            "review_text": "Approved.",
            "updated_at": "2026-08-10T04:11:00Z",
        },
    )
    rows = [
        session(1, status="completed"),
        session(2, status="completed", node="review"),
    ]
    run = compose_run(DBOS(Workflow(status)), status.workflow_id, rows, "unknown")


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


def test_review_does_not_inherit_the_implement_lane_head_reads():
    """Branch head reads belong to the implement lane alone.

    `implement_then_review` calls read_branch_head only inside the implement
    loop, so the reviewer takes no reading of its own. Pairing its single
    attempt against the run's undifferentiated observation list handed it
    implement attempt 1's pair, and the console printed implement's finding
    verbatim under review. Branch head movement is the routing signal in ADR
    038 decision 1, so evidence on the wrong node is the confusion that
    decision exists to prevent.
    """
    status = Status("wf-lanes", "SUCCESS", ["task", "repo", "main"])
    run = compose_run(
        DBOS(
            Workflow(status),
            steps=[
                {"function_name": "read_branch_head", "output": "1234567890"},
                {"function_name": "read_branch_head", "output": "abcdef0123"},
            ],
        ),
        "wf-lanes",
        [
            session(1, status="completed", node="implement", attempt=1),
            session(2, status="completed", node="review", attempt=1),
        ],
        "unknown",
    )
    implement, review = run["nodes"][0], run["nodes"][2]
    assert implement["attempts"][0]["prior_head"] == "12345678"
    assert implement["attempts"][0]["finding"]["observed_head"] == "abcdef01"
    review_attempt = review["attempts"][0]
    assert review_attempt["finding"] is None
    assert review_attempt["prior_head"] is None


def test_unserializable_timestamp_is_absent_not_passed_through():
    """A timestamp the server cannot serialize must not reach the client."""
    status = Status("wf-clock", "SUCCESS", ["task", "repo", "main"])
    status.created_at = object()
    run = compose_run(DBOS(Workflow(status)), "wf-clock", [], "unknown")
    assert run["created_at"] is None


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


def test_compose_master_bulk_loads_decisions_for_all_listed_workflows():
    workflows = [
        Workflow(Status("wf-one", "PENDING", ["one", "repo", "main"])),
        Workflow(Status("wf-two", "ENQUEUED", ["two", "repo", "main"])),
    ]

    class ListingDBOS(DBOS):
        def __init__(self):
            super().__init__(workflows[0])

        def retrieve_workflow(self, workflow_id):
            return next(
                workflow
                for workflow in workflows
                if workflow.status.workflow_id == workflow_id
            )

        def list_workflows(self, **kwargs):
            if "parent_workflow_id" in kwargs:
                return []
            return workflows

    loaded = []

    def load_decisions(workflow_ids):
        loaded.append(workflow_ids)
        return {
            "wf-two": [
                {
                    "id": 47,
                    "node_key": "review",
                    "kind": "review_escalation",
                    "options": ["retry", "send_back"],
                    "note": None,
                    "requested_at": datetime(2026, 8, 22, tzinfo=timezone.utc),
                }
            ]
        }

    result = compose_master(
        ListingDBOS(),
        True,
        {"wf-one": [], "wf-two": []},
        "unknown",
        decision_loader=load_decisions,
    )

    assert loaded == [["wf-one", "wf-two"]]
    assert [run["workflow_id"] for run in result["runs"]] == ["wf-one", "wf-two"]
    assert result["runs"][1]["current"] == {"label": "review", "state": "blocked"}


def test_master_terminal_listing_uses_limit_and_descending_sort():
    status = Status("wf-master-terminal", "SUCCESS", ["task", "repo", "main"])

    class ListingDBOS(DBOS):
        def list_workflows(self, **kwargs):
            if "parent_workflow_id" in kwargs:
                return []
            assert kwargs["limit"] == 3
            assert kwargs["sort_desc"] is True
            return [self.workflow]

    result = compose_master(
        ListingDBOS(Workflow(status)),
        False,
        {"wf-master-terminal": []},
        "unknown",
        limit=3,
    )
    assert result["runs"][0]["dbos_status"] == "SUCCESS"


def test_master_row_includes_dbos_status():
    status = Status("wf-master-status", "CANCELLED", ["task", "repo", "main"])

    class ListingDBOS(DBOS):
        def list_workflows(self, **kwargs):
            return [self.workflow]

    result = compose_master(
        ListingDBOS(Workflow(status)), True, {"wf-master-status": []}, "unknown"
    )
    assert result["runs"][0]["dbos_status"] == "CANCELLED"


def _composed_prose(run: dict) -> list[tuple[str, str]]:
    """Every string this payload composes for a human to read.

    Deliberately NOT the whole payload. Identifiers, SHAs, branch names and
    internal enums are legitimate data; the contract is only that nothing
    *composed for display* carries machine formatting. Quoted testimony is
    excluded for the same reason: it is the agent's own words, and ADR 056
    decision 7's no-filename rule governs system-composed walkthrough text,
    not what an agent said.
    """
    found: list[tuple[str, str]] = []
    disposition = run.get("disposition") or {}
    for key in ("reason", "next"):
        if isinstance(disposition.get(key), str):
            found.append((f"disposition.{key}", disposition[key]))
    for node in run.get("nodes") or []:
        current = node.get("current") or {}
        if isinstance(current.get("label"), str):
            found.append((f"nodes[{node.get('key')}].current.label", current["label"]))
        verdict = node.get("verdict") or {}
        if isinstance(verdict.get("summary_plain"), str):
            found.append(
                (f"nodes[{node.get('key')}].verdict", verdict["summary_plain"])
            )
    for deviation in run.get("deviations") or []:
        if isinstance(deviation.get("text"), str):
            found.append((f"deviations[{deviation.get('code')}]", deviation["text"]))
    return found


def test_composed_prose_carries_no_machine_formatting():
    """Pins three presentation defects shut at once.

    A raw ISO timestamp and a raw enum are both internal vocabulary reaching
    the reader. Fixing the three known instances would not stop a fourth, so
    the class is asserted rather than the instances.
    """
    iso = re.compile(r"\d{4}-\d{2}-\d{2}T")
    enum = re.compile(r"^[A-Z][A-Z_]+$")
    for status, output in (
        (
            Status("wf-a", "SUCCESS", ["task", "repo", "main"]),
            {"review_verdict": "approve"},
        ),
        (
            Status("wf-b", "SUCCESS", ["task", "repo", "main"]),
            {"review_verdict": "request_changes"},
        ),
        (Status("wf-c", "CANCELLED", ["task", "repo", "main"]), {}),
    ):
        run = compose_run(
            DBOS(Workflow(status, output)), status.workflow_id, [], "unknown"
        )
        if run is None:
            continue
        for where, value in _composed_prose(run):
            assert not iso.search(value), f"{where} carries a raw timestamp: {value}"
            assert not enum.fullmatch(value.strip()), f"{where} is a raw enum: {value}"


def test_compose_master_skips_non_run_workflows():
    """A drain_cycle workflow in the registry must not 500 the master view.

    Regression for the qwen drainer rollout: drain_cycle rows are top-level
    DBOS workflows with an empty input, and compose_master's title line
    (`task.text.splitlines()[0]`) raised IndexError on them, taking down
    GET /api/swarm/runs entirely.
    """
    run_status = Status(
        "wf-run", "PENDING", ["task", "repo", "main"], name="implement_then_review"
    )
    drain_status = Status("wf-drain", "PENDING", [], name="drain_cycle")

    class ListingDBOS(DBOS):
        def list_workflows(self, **kwargs):
            return [Workflow(drain_status), self.workflow]

        def retrieve_workflow(self, workflow_id):
            if workflow_id == run_status.workflow_id:
                return self.workflow
            return Workflow(drain_status)

    result = compose_master(
        ListingDBOS(Workflow(run_status, {"plan": FX4_EXPECTED_PLAN})),
        True,
        {run_status.workflow_id: []},
        "unknown",
    )

    assert [run["workflow_id"] for run in result["runs"]] == ["wf-run"]
