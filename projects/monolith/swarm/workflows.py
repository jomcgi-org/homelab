from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from dbos import DBOS

from swarm.policy import (
    implementer_prompt,  # noqa: F401 - retained for policy seam compatibility
    implementer_prompt_parts,
    next_action,
    parse_review_verdict,
    reviewer_prompt,  # noqa: F401 - retained for policy seam compatibility
    reviewer_prompt_parts,
    work_branch,
)
from swarm.queues import codex_queue
from swarm.steps import (
    expire_decision,
    get_decision,
    get_open_decision,
    open_decision,
    pin_plan,
    poll_turn,
    read_branch_head,
    start_agent_session,
    update_turn_shas,
)

logger = logging.getLogger(__name__)

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
    node_key: str | None = None,
    node_attempt: int | None = None,
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
    return start_agent_session(
        key,
        prompt,
        model,
        repo,
        branch,
        workflow_id,
        node_key,
        node_attempt,
    )


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
    node_key: str | None = None,
    node_attempt: int | None = None,
) -> int:
    handle = codex_queue().enqueue(
        start_session_workflow,
        key,
        prompt,
        model,
        repo,
        branch,
        workflow_id,
        node_key,
        node_attempt,
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


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _await_decision(
    workflow_id: str,
    node_key: str,
    kind: str,
    options: list[str],
    note: str,
    timeout_s: int,
) -> dict:
    """Open one durable decision request and poll it to resolution."""
    requested = open_decision(workflow_id, node_key, kind, options, note)
    deadline = _timestamp(requested["requested_at"]) + timedelta(seconds=timeout_s)

    while True:
        current = get_open_decision(workflow_id, node_key)
        if current is None:
            # Once an answer sets decided_at the row no longer matches the
            # partial open-row lookup, so read the same durable row by id.
            current = get_decision(requested["id"])
        if current is not None and current["decided_at"] is not None:
            return current
        observed_at = _timestamp((current or requested)["observed_at"])
        if observed_at >= deadline:
            expired = expire_decision(workflow_id, node_key)
            if expired is not None:
                return expired
            # A human may have answered between the poll and the expiry step.
            resolved = get_decision(requested["id"])
            if resolved is not None and resolved["decided_at"] is not None:
                return resolved
        DBOS.sleep(POLL_INTERVAL_SECONDS)


def _decision_output(row: dict) -> dict:
    return {
        "node_key": row["node_key"],
        "kind": row["kind"],
        "decision": row["decision"],
        "note": row["decision_note"],
        "actor_subject": row["actor_subject"],
        "decided_at": row["decided_at"],
    }


def _record_turn_intent(session_id: int, turn: dict | None, intent: str) -> None:
    if turn is None:
        return
    try:
        from agent_sessions import store as agent_store
        from core.db import get_engine
        from sqlmodel import Session

        with Session(get_engine()) as db_session:
            agent_store.update_turn_prompt_intent(
                db_session, session_id, turn["seq"], intent
            )
    except Exception:  # noqa: BLE001 - recording is best effort
        logger.warning(
            "failed to record turn intent for session %s seq %s",
            session_id,
            turn["seq"],
            exc_info=True,
        )


def _escalated(
    attempt: int,
    session_id: int | None,
    turn: dict | None,
    branch_name: str,
    branch_head: str | None = None,
    cost_usd: float | None = None,
    decision: dict | None = None,
) -> dict:
    # commit_sha stays None on escalation: nothing was verified for review.
    # branch_head carries the last observed remote head so a triager can see
    # the branch is non-empty (e.g. a push that became visible only after a
    # timed-out attempt, which per-attempt freshness deliberately refuses to
    # claim as this run's success).
    output = {
        "status": "escalated",
        "attempts": attempt,
        "implementer_session_id": session_id,
        "commit_sha": None,
        "branch_head": branch_head,
        "work_branch": branch_name,
        "reviewer_session_id": None,
        "review_text": None,
        "review_verdict": None,
        "cost_usd": _cost(turn) if cost_usd is None else cost_usd,
    }
    if decision is not None:
        output["decision"] = _decision_output(decision)
    return output


@DBOS.workflow()
def implement_then_review(
    task: str,
    repo: str,
    branch: str,
    budget_usd: float | None = None,
    model: str | None = None,
) -> dict:
    plan = pin_plan(budget_usd, model)
    try:
        workflow_id = DBOS.workflow_id
    except Exception:  # noqa: BLE001 - no workflow context
        workflow_id = None
    if workflow_id is not None:
        # The authoritative pin is pin_plan's own step record; this copy exists
        # only so the run endpoint can read the plan without walking steps. A
        # convenience write must never be able to kill a run at its first
        # instruction, so a failure here is logged and dropped.
        try:
            DBOS.update_workflow_attributes(workflow_id, {"plan": plan})
        except Exception:  # noqa: BLE001 - the step record is the real pin
            logger.warning(
                "failed to copy pinned plan onto workflow attributes",
                exc_info=True,
            )
    branch_name = work_branch(workflow_id or "unknown")
    max_attempts = plan["max_attempts"]
    max_review_cycles = plan["max_review_cycles"]
    attempt = 0
    previous_failure = None
    implementer_session_id = None
    implementer_turn = None
    base_sha = None
    commit_sha = None
    total_cost = 0.0
    push_gate_approved = False

    while attempt < max_attempts:
        attempt += 1
        prior_sha = read_branch_head(repo, branch_name)
        impl_intent, impl_protocol = implementer_prompt_parts(
            task, branch_name, previous_failure
        )
        implementer_session_id = _queued_session(
            session_key(f"implement-{attempt}"),
            f"{impl_intent}\n{impl_protocol}",
            plan["implementer_model"],
            repo,
            branch,
            workflow_id,
            node_key="implement",
            node_attempt=attempt,
        )
        implementer_turn = _await_turn(
            implementer_session_id, 0, plan["turn_timeout_seconds"]
        )
        total_cost += _cost(implementer_turn)
        _record_turn_intent(implementer_session_id, implementer_turn, impl_intent)
        head_sha = read_branch_head(repo, branch_name)
        base_sha = prior_sha
        if implementer_turn is not None:
            # Recording the heads is a convenience write for the walkthrough,
            # not part of the decision this loop makes: next_action reads
            # head_sha and prior_sha directly, just below. A failure here must
            # not be able to fail the attempt, for the same reason the pinned
            # plan copy above is guarded. The heads are lost for that turn and
            # the run continues.
            try:
                update_turn_shas(
                    implementer_session_id,
                    implementer_turn["seq"],
                    base_sha,
                    head_sha,
                )
            except Exception:  # noqa: BLE001 - the attempt outranks the record
                logger.warning(
                    "failed to record turn heads for session %s seq %s",
                    implementer_session_id,
                    implementer_turn["seq"],
                    exc_info=True,
                )
        action = next_action(attempt, max_attempts, head_sha, prior_sha)
        if action == "review":
            commit_sha = head_sha
            break
        if action == "escalate":
            # This is the push gate: the final implementer attempt did not
            # produce verified branch movement, so approval is the only human
            # override that can advance the run to review.
            decision = _await_decision(
                workflow_id or "unknown",
                "push_gate",
                "push_gate",
                ["approve", "send_back"],
                "No branch movement was verified after the final implementer attempt.",
                plan["decision_timeout_seconds"],
            )
            if decision["decision"] != "approve":
                return _escalated(
                    attempt,
                    implementer_session_id,
                    implementer_turn,
                    branch_name,
                    branch_head=head_sha,
                    cost_usd=total_cost,
                    decision=decision,
                )
            push_gate_approved = True
            commit_sha = read_branch_head(repo, branch_name)
            break
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

    if commit_sha is None and not push_gate_approved:
        # Defensive: next_action should have escalated already. Never fall
        # through silently into the reviewer without a commit to review. This
        # is also a push gate because review has not started.
        decision = _await_decision(
            workflow_id or "unknown",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            "No commit was available after the implementer loop.",
            plan["decision_timeout_seconds"],
        )
        if decision["decision"] != "approve":
            return _escalated(
                attempt,
                implementer_session_id,
                implementer_turn,
                branch_name,
                cost_usd=total_cost,
                decision=decision,
            )
        commit_sha = read_branch_head(repo, branch_name)

    reviewer_session_id = None
    reviewer_turn = None
    verdict = "unparseable"
    review_cycles = 0
    review_feedback = None

    while True:
        # Every reviewer gets a FRESH session (ADR 038 decision 3), and each
        # review node starts at node_attempt 1. The implementer's workspace is
        # never handed to the reviewer.
        reviewer_intent, reviewer_protocol = reviewer_prompt_parts(
            task, branch_name, commit_sha
        )
        reviewer_session_id = _queued_session(
            session_key(f"review-{review_cycles + 1}"),
            f"{reviewer_intent}\n{reviewer_protocol}",
            plan["reviewer_model"],
            repo,
            branch,
            workflow_id,
            node_key="review",
            node_attempt=1,
        )
        reviewer_turn = _await_turn(
            reviewer_session_id, 0, plan["turn_timeout_seconds"]
        )
        total_cost += _cost(reviewer_turn)
        _record_turn_intent(reviewer_session_id, reviewer_turn, reviewer_intent)
        verdict = parse_review_verdict(
            reviewer_turn.get("result_text") if reviewer_turn else None
        )

        if verdict == "request_changes" and review_cycles < max_review_cycles:
            review_cycles += 1
            review_feedback = (
                reviewer_turn.get("result_text") if reviewer_turn else None
            )
            prior_sha = read_branch_head(repo, branch_name)
            attempt += 1
            impl_intent, impl_protocol = implementer_prompt_parts(
                task,
                branch_name,
                review_feedback=review_feedback,
            )
            implementer_session_id = _queued_session(
                session_key(f"implement-{attempt}"),
                f"{impl_intent}\n{impl_protocol}",
                plan["implementer_model"],
                repo,
                branch,
                workflow_id,
                node_key="implement",
                node_attempt=attempt,
            )
            implementer_turn = _await_turn(
                implementer_session_id, 0, plan["turn_timeout_seconds"]
            )
            total_cost += _cost(implementer_turn)
            _record_turn_intent(implementer_session_id, implementer_turn, impl_intent)
            head_sha = read_branch_head(repo, branch_name)
            base_sha = prior_sha
            if implementer_turn is not None:
                try:
                    update_turn_shas(
                        implementer_session_id,
                        implementer_turn["seq"],
                        base_sha,
                        head_sha,
                    )
                except Exception:  # noqa: BLE001 - recording is best effort
                    logger.warning(
                        "failed to record turn heads for session %s seq %s",
                        implementer_session_id,
                        implementer_turn["seq"],
                        exc_info=True,
                    )
            if implementer_turn is None or not head_sha or head_sha == prior_sha:
                # Review already requested a send-back attempt, so this is a
                # review escalation rather than the pre-review push gate.
                decision = _await_decision(
                    workflow_id or "unknown",
                    "review",
                    "review_escalation",
                    ["retry", "send_back"],
                    "The send-back implementation attempt did not move the branch.",
                    plan["decision_timeout_seconds"],
                )
                if decision["decision"] != "retry":
                    return _escalated(
                        attempt,
                        implementer_session_id,
                        implementer_turn,
                        branch_name,
                        branch_head=head_sha,
                        cost_usd=total_cost,
                        decision=decision,
                    )
                head_sha = read_branch_head(repo, branch_name)
                if head_sha:
                    commit_sha = head_sha
                continue
            commit_sha = head_sha
            continue

        if verdict == "request_changes" and review_cycles >= max_review_cycles:
            return {
                "status": "review_cycles_exhausted",
                "attempts": attempt,
                "implementer_session_id": implementer_session_id,
                "commit_sha": commit_sha,
                "work_branch": branch_name,
                "reviewer_session_id": reviewer_session_id,
                "review_text": reviewer_turn.get("result_text")
                if reviewer_turn
                else None,
                "review_verdict": verdict,
                "cost_usd": total_cost,
            }
        break

    return {
        "status": "review",
        "attempts": attempt,
        "implementer_session_id": implementer_session_id,
        "commit_sha": commit_sha,
        "work_branch": branch_name,
        "reviewer_session_id": reviewer_session_id,
        "review_text": reviewer_turn.get("result_text") if reviewer_turn else None,
        "review_verdict": verdict,
        "cost_usd": total_cost,
    }


def _cost(turn: dict | None) -> float:
    return float((turn or {}).get("cost_usd") or 0)
