import graph.workflows as workflows


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
        heads = [
            turns[session].get("commit_sha")
            for session in sorted(turns)
            if session < 200
        ]
    branch_heads = iter(heads)
    monkeypatch.setattr(workflows, "codex_queue", lambda: Queue())
    monkeypatch.setattr(workflows, "start_agent_session", lambda *args: 0)
    monkeypatch.setattr(workflows, "_await_turn", lambda session, *_: turns[session])
    monkeypatch.setattr(workflows, "read_branch_head", lambda *_: next(branch_heads))
    monkeypatch.setattr(
        workflows,
        "_queued_session",
        lambda key, prompt, model, repo, branch: (
            calls.append((key, prompt, model, repo, branch)) or next(sessions)
        ),
    )
    return calls


def test_commit_on_first_attempt_goes_to_review(monkeypatch):
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            201: {"commit_sha": None, "result_text": "review", "cost_usd": 2},
        },
    )
    result = workflow("task", "jomcgi/homelab", "main")
    assert result["status"] == "review"
    assert result["attempts"] == 1
    assert result["commit_sha"] == "abc"
    assert result["work_branch"] == "claude/graph-unknown"
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
    assert len(calls) == 2


def test_queue_receives_a_workflow_not_a_step(monkeypatch):
    """Regression: DBOS queues enqueue workflows, so the concurrency cap only
    binds if what is enqueued is a workflow. Enqueuing the step directly left
    the codex cap unenforced."""
    enqueued = []

    class RecordingQueue:
        def enqueue(self, function, *args):
            enqueued.append(function)

            class Handle:
                def get_result(self):
                    return 101

            return Handle()

    monkeypatch.setattr(workflows, "codex_queue", lambda: RecordingQueue())
    workflows._queued_session("k-1", "p", "luna", "jomcgi/homelab", "main")
    assert enqueued == [workflows.start_session_workflow]
    assert enqueued[0] is not workflows.start_agent_session


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
    assert len(calls[1]) == 5
    assert all("lineage" not in str(value) for value in calls[1])


def test_routes_on_github_head_not_turn_commit(monkeypatch):
    """The turn CLAIMS a commit; GitHub says the branch was never pushed. The
    graph must believe GitHub. Routing on the turn field is what made the
    reviewer unreachable, because AgentTurn.commit_sha is never written."""
    calls = run(
        monkeypatch,
        {
            101: {"commit_sha": "abc", "result_text": "done", "cost_usd": 1},
            102: {"commit_sha": "def", "result_text": "done", "cost_usd": 1},
        },
        heads=[None, None],
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
