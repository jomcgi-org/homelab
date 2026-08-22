import pytest

import swarm.workflows as workflows
from swarm import config


class Queue:
    def enqueue(self, function, *args):
        class Handle:
            def get_result(self):
                return function(*args)

        return Handle()


def workflow(*args, **kwargs):
    # DBOS requires an initialized runtime to invoke the decorated entrypoint.
    # The wrapped function is the deterministic control flow under test, while
    # the decorator remains on the production entrypoint.
    return workflows.implement_then_review.__wrapped__(*args, **kwargs)


def decision_row(
    decision="expired",
    *,
    node_key="push_gate",
    kind="push_gate",
    note=None,
    actor_subject=None,
    requested_at="2026-08-22T00:00:00+00:00",
    decided_at="2026-08-22T00:00:05+00:00",
    observed_at="2026-08-22T00:00:05+00:00",
):
    return {
        "id": 1,
        "workflow_id": "wf-1",
        "node_key": node_key,
        "kind": kind,
        "options": ["approve", "send_back"],
        "note": "needs a decision",
        "requested_at": requested_at,
        "decided_at": decided_at,
        "decision": decision,
        "decision_note": note,
        "actor_subject": actor_subject,
        "actor_authority": "cloudflare" if actor_subject else None,
        "observed_at": observed_at,
    }


def run(
    monkeypatch,
    turns,
    heads=None,
    intents=None,
    decisions=None,
    decision_timeout=60,
):
    sessions = iter(turns)
    calls = []
    if heads is None:
        heads = []
        for session in sorted(turns):
            if session < 200:
                heads.extend([None, turns[session].get("commit_sha")])
    branch_heads = iter(heads)
    monkeypatch.setattr(workflows, "codex_queue", lambda: Queue())

    class FakeDBOS:
        workflow_id = "wf-1"

        @staticmethod
        def update_workflow_attributes(workflow_id, values):
            pass

    monkeypatch.setattr(workflows, "DBOS", FakeDBOS)
    monkeypatch.setattr(
        workflows,
        "pin_plan",
        lambda budget_usd=None, model=None: {
            "version": 2,
            "max_attempts": max(1, config.max_attempts()),
            "max_review_cycles": max(1, config.max_review_cycles()),
            "implementer_model": model or config.implementer_model(),
            "reviewer_model": config.reviewer_model(),
            "turn_timeout_seconds": config.turn_timeout_seconds(),
            "decision_timeout_seconds": decision_timeout,
            "budget_usd": budget_usd,
        },
    )
    monkeypatch.setattr(workflows, "start_agent_session", lambda *args: 0)
    monkeypatch.setattr(workflows, "update_turn_shas", lambda *args: True)
    decision_results = iter(decisions or [decision_row()])
    monkeypatch.setattr(
        workflows, "_await_decision", lambda *args: next(decision_results)
    )
    if intents is not None:
        monkeypatch.setattr(
            workflows,
            "_record_turn_intent",
            lambda session_id, turn, intent: intents.append(
                (session_id, turn["seq"], intent)
            ),
        )
    # seq is part of poll_turn's real contract (swarm/steps.py), so the stub
    # carries it too. Defaulted rather than added to every literal below, and
    # overridable by any stub that cares which turn it is.
    monkeypatch.setattr(
        workflows,
        "_await_turn",
        lambda session, *_: {"seq": 0, **turns[session]},
    )
    monkeypatch.setattr(workflows, "read_branch_head", lambda *_: next(branch_heads))
    monkeypatch.setattr(
        workflows,
        "_queued_session",
        lambda key, prompt, model, repo, branch, workflow_id, node_key=None, node_attempt=None: (
            calls.append(
                (key, prompt, model, repo, branch, workflow_id, node_key, node_attempt)
            )
            or next(sessions)
        ),
    )
    return calls


def test_commit_on_first_attempt_goes_to_review(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "review\nVERDICT: APPROVE",
                "cost_usd": 2,
            },
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review"
    assert result["attempts"] == 1
    assert result["commit_sha"] == "abc"
    assert result["review_verdict"] == "approve"
    assert result["work_branch"] == "claude/swarm-wf-1"
    assert len(calls) == 2
    assert calls[0][-2:] == ("implement", 1)
    assert calls[1][-2:] == ("review", 1)


def test_explicit_model_is_used_by_implementer(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
    )

    workflow("task", "jomcgi/homelab", "main", model="terra")

    assert calls[0][2] == "terra"


def test_no_commit_retries(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "review", "cost_usd": 1},
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["attempts"] == 2
    assert len(calls) == 3
    assert [call[-2:] for call in calls] == [
        ("implement", 1),
        ("implement", 2),
        ("review", 1),
    ]


def test_pinned_attempt_bound_survives_config_change(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "2")
    # This test is about the attempt bound, not the pause: keep escalation
    # terminal so the expected plan and the run path stay deterministic.
    monkeypatch.setenv("SWARM_DECISION_TIMEOUT_SECONDS", "0")
    attributes = []
    sessions = iter([101, 102])
    heads = iter([None, None, None, None])
    monkeypatch.setattr(
        workflows,
        "pin_plan",
        lambda budget_usd=None, model=None: {
            "version": 2,
            "max_attempts": max(1, config.max_attempts()),
            "max_review_cycles": max(1, config.max_review_cycles()),
            "implementer_model": config.implementer_model(),
            "reviewer_model": config.reviewer_model(),
            "turn_timeout_seconds": config.turn_timeout_seconds(),
            "decision_timeout_seconds": config.decision_timeout_seconds(),
            "budget_usd": budget_usd,
        },
    )

    class FakeDBOS:
        workflow_id = "wf-1"

        @staticmethod
        def update_workflow_attributes(workflow_id, values):
            attributes.append((workflow_id, values))

    monkeypatch.setattr(workflows, "DBOS", FakeDBOS)
    monkeypatch.setattr(workflows, "read_branch_head", lambda *_: next(heads))
    monkeypatch.setattr(
        workflows,
        "_queued_session",
        lambda *args, **kwargs: next(sessions),
    )
    monkeypatch.setattr(
        workflows,
        "_await_turn",
        lambda session, *_: (
            monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "5")
            or {"seq": 0, "result_text": "no", "cost_usd": 1}
        ),
    )
    monkeypatch.setattr(workflows, "_await_decision", lambda *args: decision_row())

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert result["attempts"] == 2
    assert attributes == [
        (
            "wf-1",
            {
                "plan": {
                    "version": 2,
                    "max_attempts": 2,
                    "max_review_cycles": 2,
                    "implementer_model": "luna",
                    "reviewer_model": "opus",
                    "turn_timeout_seconds": 1800,
                    "decision_timeout_seconds": 0,
                    "budget_usd": None,
                }
            },
        )
    ]


def test_recovered_plan_without_decision_timeout_uses_current_config(monkeypatch):
    monkeypatch.setenv("SWARM_DECISION_TIMEOUT_SECONDS", "45")
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
    )
    monkeypatch.setattr(
        workflows,
        "pin_plan",
        lambda budget_usd=None, model=None: {
            "version": 1,
            "max_attempts": 2,
            "max_review_cycles": 2,
            "implementer_model": "luna",
            "reviewer_model": "opus",
            "turn_timeout_seconds": 1800,
            "budget_usd": budget_usd,
        },
    )
    decision_calls = []
    monkeypatch.setattr(
        workflows,
        "_await_decision",
        lambda *args: decision_calls.append(args) or decision_row(),
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert decision_calls[0][-1] == 45
    assert len(calls) == 2


def test_missing_dbos_workflow_id_fails_visibly(monkeypatch):
    monkeypatch.setattr(workflows, "pin_plan", lambda *args: {})
    monkeypatch.setattr(workflows, "DBOS", object())

    with pytest.raises(RuntimeError, match="DBOS workflow id is unavailable"):
        workflow("task", "jomcgi/homelab", "main")


def test_attributes_write_failure_does_not_kill_the_run(monkeypatch):
    # The authoritative pin is pin_plan's step record; the attributes copy is a
    # convenience for the run endpoint. A convenience write must not be able to
    # kill a run at its first instruction.
    class FailingDBOS:
        workflow_id = "wf-1"

        @staticmethod
        def update_workflow_attributes(workflow_id, values):
            raise RuntimeError("attributes backend down")

    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
    )
    monkeypatch.setattr(workflows, "DBOS", FailingDBOS)

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert len(calls) == 2


def test_exhausted_attempts_escalate_without_reviewer(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "escalated"
    assert result["reviewer_session_id"] is None
    assert result["review_verdict"] is None
    assert result["branch_head"] is None
    assert result["decision"]["decision"] == "expired"
    assert len(calls) == 2


def test_zero_decision_timeout_keeps_escalation_terminal(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
        decision_timeout=0,
    )
    decision_calls = []
    monkeypatch.setattr(
        workflows,
        "_await_decision",
        lambda *args: decision_calls.append(args) or decision_row(),
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert "decision" not in result
    assert decision_calls == []
    assert len(calls) == 2


def test_push_gate_pause_then_approve_resumes_review(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: APPROVE",
                "cost_usd": 1,
            },
        },
        heads=[None, None, None, None, "abc"],
        decisions=[decision_row("approve", actor_subject="joe@example.com")],
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "review"
    assert result["commit_sha"] == "abc"
    assert result["review_verdict"] == "approve"
    assert [call[-2:] for call in calls] == [
        ("implement", 1),
        ("implement", 2),
        ("review", 1),
    ]


def test_push_gate_approve_without_commit_still_escalates(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
        heads=[None, None, None, None, None],
        decisions=[decision_row("approve", actor_subject="joe@example.com")],
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert result["commit_sha"] is None
    assert result["decision"]["decision"] == "approve"
    assert [call[-2:] for call in calls] == [
        ("implement", 1),
        ("implement", 2),
    ]


def test_send_back_ends_run_with_human_note(monkeypatch):
    run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
        decisions=[
            decision_row(
                "send_back",
                note="Please push the missing change.",
                actor_subject="joe@example.com",
            )
        ],
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert result["decision"] == {
        "decision_id": 1,
        "node_key": "push_gate",
        "kind": "push_gate",
        "decision": "send_back",
        "ask": "needs a decision",
        "decision_note": "Please push the missing change.",
        "actor_subject": "joe@example.com",
        "actor_authority": "cloudflare",
        "decided_at": "2026-08-22T00:00:05+00:00",
    }


def test_review_escalation_retry_runs_implementer_before_reviewer(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "3")
    decision_calls = []
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": None, "result_text": "no push", "cost_usd": 1},
            103: {"commit_sha": "def", "result_text": "fixed", "cost_usd": 1},
            202: {
                "commit_sha": None,
                "result_text": "VERDICT: APPROVE",
                "cost_usd": 1,
            },
        },
        heads=[None, "abc", "abc", "abc", "abc", "def"],
    )
    retry = decision_row("retry", node_key="review", kind="review_escalation")
    retry["options"] = ["retry", "send_back"]
    monkeypatch.setattr(
        workflows,
        "_await_decision",
        lambda *args: decision_calls.append(args) or retry,
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "review"
    assert result["commit_sha"] == "def"
    assert result["review_verdict"] == "approve"
    assert [call[-2:] for call in calls] == [
        ("implement", 1),
        ("review", 1),
        ("implement", 2),
        ("implement", 3),
        ("review", 1),
    ]
    assert decision_calls[0][1:4] == (
        "review",
        "review_escalation",
        ["retry", "send_back"],
    )


def test_review_escalation_retry_at_attempt_limit_records_decision(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": None, "result_text": "no push", "cost_usd": 1},
        },
        heads=[None, "abc", "abc", "abc"],
        decisions=[decision_row("retry", node_key="review", kind="review_escalation")],
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "review_cycles_exhausted"
    assert result["decision"]["decision"] == "retry"
    assert [call[-2:] for call in calls] == [
        ("implement", 1),
        ("review", 1),
        ("implement", 2),
    ]


def test_await_decision_polls_then_resumes_on_approve(monkeypatch):
    open_row = decision_row(
        None,
        decided_at=None,
        observed_at="2026-08-22T00:00:01+00:00",
    )
    approved = decision_row("approve", actor_subject="joe@example.com")
    open_polls = iter([open_row, None])
    sleeps = []

    monkeypatch.setattr(workflows, "open_decision", lambda *args: open_row)
    monkeypatch.setattr(workflows, "get_open_decision", lambda *args: next(open_polls))
    monkeypatch.setattr(workflows, "get_decision", lambda decision_id: approved)
    monkeypatch.setattr(
        workflows,
        "DBOS",
        type("FakeDBOS", (), {"sleep": staticmethod(sleeps.append)}),
    )

    result = workflows._await_decision(
        "wf-1", "push_gate", "push_gate", ["approve", "send_back"], "note", 60
    )

    assert result["decision"] == "approve"
    assert sleeps == [workflows.DECISION_POLL_INTERVAL_SECONDS]


def test_await_decision_expires_at_pinned_timeout(monkeypatch):
    open_row = decision_row(
        None,
        decided_at=None,
        requested_at="2026-08-22T00:00:00+00:00",
        observed_at="2026-08-22T00:00:10+00:00",
    )
    expired = decision_row("expired")
    expired_calls = []

    monkeypatch.setattr(workflows, "open_decision", lambda *args: open_row)
    monkeypatch.setattr(workflows, "get_open_decision", lambda *args: open_row)
    monkeypatch.setattr(
        workflows,
        "expire_decision",
        lambda *args: expired_calls.append(args) or expired,
    )

    result = workflows._await_decision(
        "wf-1", "push_gate", "push_gate", ["approve", "send_back"], "note", 5
    )

    assert result["decision"] == "expired"
    assert expired_calls == [("wf-1", "push_gate")]


def test_await_decision_returns_expired_when_row_is_deleted(monkeypatch):
    open_row = decision_row(
        None,
        decided_at=None,
        observed_at="2026-08-22T00:00:01+00:00",
    )
    expire_calls = []

    monkeypatch.setattr(workflows, "open_decision", lambda *args: open_row)
    monkeypatch.setattr(workflows, "get_open_decision", lambda *args: None)
    monkeypatch.setattr(workflows, "get_decision", lambda *args: None)
    monkeypatch.setattr(
        workflows,
        "expire_decision",
        lambda *args: expire_calls.append(args) or None,
    )

    result = workflows._await_decision(
        "wf-1", "push_gate", "push_gate", ["approve", "send_back"], "note", 60
    )

    assert result["decision"] == "expired"
    assert expire_calls == [("wf-1", "push_gate")]


def test_await_decision_iteration_cap_terminates_stalled_clock(monkeypatch):
    open_row = decision_row(
        None,
        decided_at=None,
        observed_at="2026-08-22T00:00:01+00:00",
    )
    polls = []
    sleeps = []

    monkeypatch.setattr(workflows, "open_decision", lambda *args: open_row)
    monkeypatch.setattr(
        workflows,
        "get_open_decision",
        lambda *args: polls.append(args) or open_row,
    )
    monkeypatch.setattr(
        workflows,
        "DBOS",
        type("FakeDBOS", (), {"sleep": staticmethod(sleeps.append)}),
    )

    result = workflows._await_decision(
        "wf-1", "push_gate", "push_gate", ["approve", "send_back"], "note", 60
    )

    assert result["decision"] == "expired"
    assert len(polls) == 5
    assert sleeps == [workflows.DECISION_POLL_INTERVAL_SECONDS] * 4


def test_await_decision_replay_reuses_open_row(monkeypatch):
    existing = decision_row(
        None,
        decided_at=None,
        observed_at="2026-08-22T00:00:01+00:00",
    )
    existing["id"] = 7
    approved = {**decision_row("approve"), "id": 7}
    open_calls = []
    open_polls = iter([existing, None])

    monkeypatch.setattr(
        workflows,
        "open_decision",
        lambda *args: open_calls.append(args) or existing,
    )
    monkeypatch.setattr(workflows, "get_open_decision", lambda *args: next(open_polls))
    monkeypatch.setattr(workflows, "get_decision", lambda decision_id: approved)
    monkeypatch.setattr(
        workflows,
        "DBOS",
        type("FakeDBOS", (), {"sleep": staticmethod(lambda seconds: None)}),
    )

    result = workflows._await_decision(
        "wf-1", "push_gate", "push_gate", ["approve", "send_back"], "note", 60
    )

    assert result["id"] == 7
    assert result["decision"] == "approve"
    assert len(open_calls) == 1


def test_preexisting_unmoved_branch_retries_then_escalates_with_branch_head(
    monkeypatch,
):
    """A branch left behind by an earlier run (or a push that became visible
    late) must not count as this attempt's success, but the escalation must
    surface the observed head so a triager can see the branch is non-empty,
    and the retry feedback must describe an unmoved branch, not a missing one.
    """
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": None, "result_text": "no", "cost_usd": 1},
        },
        heads=["abc", "abc", "abc", "abc"],
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "escalated"
    assert result["commit_sha"] is None
    assert result["branch_head"] == "abc"
    retry_prompt = calls[1][1]
    assert "did not move" in retry_prompt
    assert "not found" not in retry_prompt


def test_queue_receives_a_workflow_not_a_step(monkeypatch):
    """Regression: DBOS queues enqueue workflows, so the concurrency cap only
    binds if what is enqueued is a workflow. Enqueuing the step directly left
    the codex cap unenforced."""
    enqueued = []
    arguments = []

    class RecordingQueue:
        def enqueue(self, function, *args):
            enqueued.append(function)
            arguments.append(args)

            class Handle:
                def get_result(self):
                    return 101

            return Handle()

    monkeypatch.setattr(workflows, "codex_queue", lambda: RecordingQueue())
    workflows._queued_session("k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123")
    assert enqueued == [workflows.start_session_workflow]
    assert enqueued[0] is not workflows.start_agent_session
    assert arguments == [
        ("k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123", None, None)
    ]


def test_start_session_workflow_forwards_workflow_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        workflows,
        "start_agent_session",
        lambda *args: calls.append(args) or 101,
    )

    result = workflows.start_session_workflow.__wrapped__(
        "k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123"
    )

    assert result == 101
    assert calls == [
        ("k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123", None, None)
    ]


def test_empty_commit_sha_is_not_success(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "", "result_text": "no", "cost_usd": 1},
            102: {"commit_sha": "", "result_text": "no", "cost_usd": 1},
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "escalated"
    assert len(calls) == 2


def test_reviewer_has_no_lineage_argument(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "review", "cost_usd": 1},
        },
    )
    workflow("task", "jomcgi/homelab", "main")
    # Six positional plus node_key and node_attempt. The arity is asserted so a
    # NEW argument cannot appear unnoticed; the substring check below is the
    # property that actually matters (ADR 038 decision 3: the reviewer is never
    # handed the implementer's lineage).
    assert len(calls[1]) == 8
    assert all("lineage" not in str(value) for value in calls[1])


def test_routes_on_github_head_not_turn_commit(monkeypatch):
    """The turn CLAIMS a commit; GitHub says the branch was never pushed. The
    swarm must believe GitHub. Routing on the turn field is what made the
    reviewer unreachable, because AgentTurn.commit_sha is never written."""
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
        },
        heads=[None, None, None, None],
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "escalated"
    assert result["reviewer_session_id"] is None
    assert result["commit_sha"] is None
    # Two implementer attempts, and never a reviewer, despite both turns
    # self-reporting a commit sha.
    assert len(calls) == 2


def test_session_keys_are_deterministic_and_distinct(monkeypatch):
    """Steps are at-least-once, so the session key is the idempotency key. A
    retried step that minted a fresh uuid left one live agent session per
    attempt, each holding a Codex slot."""
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": None, "result_text": "no", "cost_usd": 0},
            102: {"commit_sha": "abc", "result_text": "done", "cost_usd": 0},
            201: {"commit_sha": None, "result_text": "review", "cost_usd": 0},
        },
    )
    workflow("task", "jomcgi/homelab", "main")
    keys = [call[0] for call in calls]
    # One key per node, stable across a re-run of the same workflow, and the
    # reviewer never shares a key with an implementer attempt.
    assert keys == [
        workflows.session_key("implement-1"),
        workflows.session_key("implement-2"),
        workflows.session_key("review-1"),
    ]
    assert len(set(keys)) == 3


def test_unparseable_reviewer_reply_is_returned_without_crashing(monkeypatch):
    run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "looks good", "cost_usd": 1},
        },
        heads=[None, "abc"],
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["review_verdict"] == "unparseable"


def test_request_changes_triggers_new_implement_attempt(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "Needs a fix\nVERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
            202: {
                "commit_sha": None,
                "result_text": "Looks good\nVERDICT: APPROVE",
                "cost_usd": 1,
            },
        },
        heads=[None, "abc", "abc", "def"],
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review"
    assert result["attempts"] == 2
    assert result["commit_sha"] == "def"
    assert result["review_verdict"] == "approve"
    assert result["cost_usd"] == 4
    assert [(call[-2], call[-1]) for call in calls] == [
        ("implement", 1),
        ("review", 1),
        ("implement", 2),
        ("review", 1),
    ]
    assert calls[1][0] == workflows.session_key("review-1")
    assert calls[3][0] == workflows.session_key("review-2")


def test_review_cycles_exhausted_on_persistent_request_changes(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_REVIEW_CYCLES", "1")
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
            202: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
        },
        heads=[None, "abc", "abc", "def"],
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review_cycles_exhausted"
    assert result["review_verdict"] == "request_changes"
    assert result["attempts"] == 2
    assert len(calls) == 4


def test_pinned_review_cycle_bound_survives_config_change(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_REVIEW_CYCLES", "1")
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
            202: {
                "commit_sha": None,
                "result_text": "VERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
        },
        heads=[None, "abc", "abc", "def"],
    )
    original_await = workflows._await_turn

    def await_turn(session, *args):
        turn = original_await(session, *args)
        if session == 201:
            monkeypatch.setenv("SWARM_MAX_REVIEW_CYCLES", "0")
        return turn

    monkeypatch.setattr(workflows, "_await_turn", await_turn)
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review_cycles_exhausted"
    assert len(calls) == 4


def test_unparseable_verdict_does_not_trigger_requeue(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "Needs work", "cost_usd": 1},
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review"
    assert result["review_verdict"] == "unparseable"
    assert len(calls) == 2


def test_review_feedback_passed_to_implementer_prompt(monkeypatch):
    feedback = []
    original_parts = workflows.implementer_prompt_parts

    def capture_prompt_parts(task, branch, previous_failure=None, review_feedback=None):
        feedback.append(review_feedback)
        return original_parts(task, branch, previous_failure, review_feedback)

    monkeypatch.setattr(workflows, "implementer_prompt_parts", capture_prompt_parts)
    run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "Fix X\nVERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
            202: {"commit_sha": None, "result_text": "VERDICT: APPROVE", "cost_usd": 1},
        },
        heads=[None, "abc", "abc", "def"],
    )
    workflow("task", "jomcgi/homelab", "main")
    assert feedback == [None, "Fix X\nVERDICT: REQUEST_CHANGES"]


def test_implementer_stores_intent(monkeypatch):
    intents = []
    run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "VERDICT: APPROVE",
                "cost_usd": 1,
            },
        },
        intents=intents,
    )

    workflow("task", "jomcgi/homelab", "main")

    assert intents[0] == (
        101,
        0,
        workflows.implementer_prompt_parts("task", "claude/swarm-wf-1")[0],
    )


def test_reviewer_stores_intent(monkeypatch):
    intents = []
    run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "VERDICT: APPROVE", "cost_usd": 1},
        },
        intents=intents,
    )

    workflow("task", "jomcgi/homelab", "main")

    assert intents[1] == (
        201,
        0,
        workflows.reviewer_prompt_parts("task", "claude/swarm-wf-1", "abc")[0],
    )


def test_implementer_feedback_loop_stores_intent(monkeypatch):
    intents = []
    run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {
                "commit_sha": None,
                "result_text": "Fix X\nVERDICT: REQUEST_CHANGES",
                "cost_usd": 1,
            },
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
            202: {
                "commit_sha": None,
                "result_text": "VERDICT: APPROVE",
                "cost_usd": 1,
            },
        },
        heads=[None, "abc", "abc", "def"],
        intents=intents,
    )

    workflow("task", "jomcgi/homelab", "main")

    expected = workflows.implementer_prompt_parts(
        "task",
        "claude/swarm-wf-1",
        review_feedback="Fix X\nVERDICT: REQUEST_CHANGES",
    )[0]
    assert intents[2] == (102, 0, expected)
