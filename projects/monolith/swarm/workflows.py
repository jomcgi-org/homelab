from __future__ import annotations

from dbos import DBOS

from swarm import config
from swarm.policy import (
    implementer_prompt,
    next_action,
    parse_review_verdict,
    reviewer_prompt,
    work_branch,
)
from swarm.queues import codex_queue
from swarm.steps import poll_turn, read_branch_head, start_agent_session

# How long to wait between polls for a session's next turn. Each sleep is a
# durable checkpoint, so the interval also sets how much wait is re-done after a
# process restart.
POLL_INTERVAL_SECONDS = 5


@DBOS.workflow()
def start_session_workflow(
    key: str,
    prompt: str,
    model: str,
    repo: str,
    branch: str,
    workflow_id: str | None = None,
) -> int:
    """Queue-able wrapper around the session-start step.

    DBOS queues enqueue WORKFLOWS, not steps, so the concurrency cap only
    applies if what we enqueue is a workflow. Enqueuing the step directly would
    not be gated by the queue at all.

    The workflow_id is passed explicitly because DBOS.workflow_id inside this
    function is the queued session workflow's id, not the caller's run id. The
    parent's id must be threaded from the caller, not read from context, or the
    session would record the wrong parent link.
    """
    return start_agent_session(key, prompt, model, repo, branch, workflow_id)


def session_key(suffix: str) -> str:
    """Deterministic idempotency key for one node of the current workflow.

    Reading DBOS.workflow_id raises outside a workflow context, so this degrades
    to a stable placeholder rather than exploding: the key only has to be
    consistent across retries OF THE SAME RUN.
    """
    try:
        workflow_id = DBOS.workflow_id
    except Exception:  # noqa: BLE001 - no workflow context
        workflow_id = None
    return f"{workflow_id}-{suffix}"


def _queued_session(
    key: str,
    prompt: str,
    model: str,
    repo: str,
    branch: str,
    workflow_id: str,
) -> int:
    handle = codex_queue().enqueue(
        start_session_workflow, key, prompt, model, repo, branch, workflow_id
    )
    return handle.get_result()


def _await_turn(session_id: int, after_seq: int, timeout_s: int) -> dict | None:
    """Wait for a session's next turn, checkpointing between polls."""
    waited = 0
    while waited < timeout_s:
        turn = poll_turn(session_id, after_seq)
        if turn is not None:
            return turn
        DBOS.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    return None


def _escalated(
    attempt: int,
    session_id: int | None,
    turn: dict | None,
    branch_name: str,
    branch_head: str | None = None,
) -> dict:
    # commit_sha stays None on escalation: nothing was verified for review.
    # branch_head carries the last observed remote head so a triager can see
    # the branch is non-empty (e.g. a push that became visible only after a
    # timed-out attempt, which per-attempt freshness deliberately refuses to
    # claim as this run's success).
    return {
        "status": "escalated",
        "attempts": attempt,
        "implementer_session_id": session_id,
        "commit_sha": None,
        "branch_head": branch_head,
        "work_branch": branch_name,
        "reviewer_session_id": None,
        "review_text": None,
        "review_verdict": None,
        "cost_usd": _cost(turn),
    }


@DBOS.workflow()
def implement_then_review(task: str, repo: str, branch: str) -> dict:
    try:
        workflow_id = DBOS.workflow_id
    except Exception:  # noqa: BLE001 - no workflow context
        workflow_id = None
    branch_name = work_branch(workflow_id or "unknown")
    max_attempts = max(1, config.max_attempts())
    attempt = 0
    previous_failure = None
    implementer_session_id = None
    implementer_turn = None
    commit_sha = None

    while attempt < max_attempts:
        attempt += 1
        prior_sha = read_branch_head(repo, branch_name)
        implementer_session_id = _queued_session(
            session_key(f"implement-{attempt}"),
            implementer_prompt(task, branch_name, previous_failure),
            config.implementer_model(),
            repo,
            branch,
            workflow_id,
        )
        implementer_turn = _await_turn(
            implementer_session_id, 0, config.turn_timeout_seconds()
        )
        head_sha = read_branch_head(repo, branch_name)
        action = next_action(attempt, max_attempts, head_sha, prior_sha)
        if action == "review":
            commit_sha = head_sha
            break
        if action == "escalate":
            return _escalated(
                attempt,
                implementer_session_id,
                implementer_turn,
                branch_name,
                branch_head=head_sha,
            )
        if head_sha:
            previous_failure = (
                f"The branch {branch_name} exists on the remote but its head "
                f"did not move during your turn (still {head_sha}). Your work "
                "was not committed and pushed. Add your changes as a new "
                "commit on top of that branch and push it this time."
            )
        else:
            previous_failure = (
                f"The branch {branch_name} was not found on the remote after "
                "the turn completed, so the work was not pushed. Push it this "
                "time."
            )

    if commit_sha is None:
        # Defensive: next_action should have escalated already. Never fall
        # through into the reviewer without a commit to review.
        return _escalated(
            attempt, implementer_session_id, implementer_turn, branch_name
        )

    # The reviewer gets a FRESH session (ADR 038 decision 3). It is never
    # handed the implementer's lineage: a prompt-injected implementer could
    # plant CLAUDE.md, AGENTS.md, or a git hook that the reviewer's CLI would
    # read as instructions, so an inherited workspace would let the auditee
    # prepare its auditor's room.
    reviewer_session_id = _queued_session(
        session_key("review"),
        reviewer_prompt(task, branch_name, commit_sha),
        config.reviewer_model(),
        repo,
        branch,
        workflow_id,
    )
    reviewer_turn = _await_turn(reviewer_session_id, 0, config.turn_timeout_seconds())
    return {
        "status": "review",
        "attempts": attempt,
        "implementer_session_id": implementer_session_id,
        "commit_sha": commit_sha,
        "work_branch": branch_name,
        "reviewer_session_id": reviewer_session_id,
        "review_text": reviewer_turn.get("result_text") if reviewer_turn else None,
        "review_verdict": parse_review_verdict(
            reviewer_turn.get("result_text") if reviewer_turn else None
        ),
        "cost_usd": _cost(implementer_turn) + _cost(reviewer_turn),
    }


def _cost(turn: dict | None) -> float:
    return float((turn or {}).get("cost_usd") or 0)
