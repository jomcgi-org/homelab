import swarm.workflows as workflows
from swarm import config


class Queue:
    def enqueue(self, function, *args):
        class Handle:
            def get_result(self):
                return function(*args)

        return Handle()


def workflow(*args):
    # DBOS requires an initialized runtime to invoke the decorated entrypoint.
    # The wrapped function is the deterministic control flow under test, while
    # the decorator remains on the production entrypoint.
    return workflows.implement_then_review.__wrapped__(*args)


def run(monkeypatch, turns, heads=None):
    sessions = iter(turns)
    calls = []
    if heads is None:
        heads = []
        for session in sorted(turns):
            if session < 200:
                heads.extend([None, turns[session].get("commit_sha")])
    branch_heads = iter(heads)
    monkeypatch.setattr(workflows, "codex_queue", lambda: Queue())
    monkeypatch.setattr(
        workflows,
        "pin_plan",
        lambda budget_usd=None: {
            "version": 1,
            "max_attempts": max(1, config.max_attempts()),
            "implementer_model": config.implementer_model(),
            "reviewer_model": config.reviewer_model(),
            "turn_timeout_seconds": config.turn_timeout_seconds(),
            "budget_usd": budget_usd,
        },
    )
    monkeypatch.setattr(workflows, "start_agent_session", lambda *args: 0)
    monkeypatch.setattr(workflows, "_await_turn", lambda session, *_: turns[session])
    monkeypatch.setattr(workflows, "read_branch_head", lambda *_: next(branch_heads))
    monkeypatch.setattr(
        workflows,
        "_queued_session",
        lambda key, prompt, model, repo, branch, workflow_id: (
            calls.append((key, prompt, model, repo, branch, workflow_id))
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
    assert result["work_branch"] == "claude/swarm-unknown"
    assert len(calls) == 2


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


def test_pinned_attempt_bound_survives_config_change(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "2")
    attributes = []
    sessions = iter([101, 102])
    heads = iter([None, None, None, None])
    monkeypatch.setattr(
        workflows,
        "pin_plan",
        lambda budget_usd=None: {
            "version": 1,
            "max_attempts": max(1, config.max_attempts()),
            "implementer_model": config.implementer_model(),
            "reviewer_model": config.reviewer_model(),
            "turn_timeout_seconds": config.turn_timeout_seconds(),
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
        lambda *args: next(sessions),
    )
    monkeypatch.setattr(
        workflows,
        "_await_turn",
        lambda session, *_: (
            monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "5")
            or {"result_text": "no", "cost_usd": 1}
        ),
    )

    result = workflow("task", "jomcgi/homelab", "main")

    assert result["status"] == "escalated"
    assert result["attempts"] == 2
    assert attributes == [
        (
            "wf-1",
            {
                "plan": {
                    "version": 1,
                    "max_attempts": 2,
                    "implementer_model": "luna",
                    "reviewer_model": "opus",
                    "turn_timeout_seconds": 1800,
                    "budget_usd": None,
                }
            },
        )
    ]


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
    assert len(calls) == 2


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
    assert arguments == [("k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123")]


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
    assert calls == [("k-1", "p", "luna", "jomcgi/homelab", "main", "wf-123")]


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
    assert len(calls[1]) == 6
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
        workflows.session_key("review"),
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
