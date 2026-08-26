from __future__ import annotations

from datetime import datetime, timezone
import os

import httpx
from dbos import DBOS


def _decision_payload(row, observed_at=None) -> dict:
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "node_key": row.node_key,
        "kind": row.kind,
        "options": row.options,
        "note": row.note,
        "requested_at": row.requested_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decision": row.decision,
        "decision_note": row.decision_note,
        "actor_subject": row.actor_subject,
        "actor_authority": row.actor_authority,
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
    }


@DBOS.step()
def pin_plan(budget_usd: float | None = None, model: str | None = None) -> dict:
    """Resolve run config ONCE and checkpoint it.

    The workflow body must never read config.* directly: recovery re-executes
    the body and a changed env would alter a live run's control flow (#4618).
    The step record is the pin; replay returns the recorded dict.
    """
    from swarm import config

    return {
        "version": 2,
        "max_attempts": max(1, config.max_attempts()),
        "max_review_cycles": max(1, config.max_review_cycles()),
        "implementer_model": model or config.implementer_model(),
        "reviewer_model": config.reviewer_model(),
        "turn_timeout_seconds": config.turn_timeout_seconds(),
        "decision_timeout_seconds": config.decision_timeout_seconds(),
        "budget_usd": budget_usd,
    }


@DBOS.step()
def open_decision(
    workflow_id: str,
    node_key: str,
    kind: str,
    options: list[str],
    note: str | None,
) -> dict:
    from sqlmodel import Session

    from core.db import get_engine
    from swarm import store

    with Session(get_engine()) as session:
        row = store.open_decision(session, workflow_id, node_key, kind, options, note)
        return _decision_payload(row, datetime.now(timezone.utc))


@DBOS.step()
def get_open_decision(workflow_id: str, node_key: str) -> dict | None:
    from sqlmodel import Session

    from core.db import get_engine
    from swarm import store

    with Session(get_engine()) as session:
        row = store.get_open_decision(session, workflow_id, node_key)
        if row is None:
            return None
        return _decision_payload(row, datetime.now(timezone.utc))


@DBOS.step()
def get_decision(decision_id: int) -> dict | None:
    from sqlmodel import Session

    from core.db import get_engine
    from swarm.models import SwarmDecision

    with Session(get_engine()) as session:
        row = session.get(SwarmDecision, decision_id)
        if row is None:
            return None
        return _decision_payload(row, datetime.now(timezone.utc))


@DBOS.step()
def expire_decision(workflow_id: str, node_key: str) -> dict | None:
    from sqlmodel import Session

    from core.db import get_engine
    from swarm import store

    with Session(get_engine()) as session:
        row = store.expire_decision(session, workflow_id, node_key)
        if row is None:
            return None
        return _decision_payload(row, datetime.now(timezone.utc))


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def start_agent_session(
    session_key,
    prompt,
    model,
    repo,
    branch,
    workflow_id: str,
    node_key: str | None = None,
    node_attempt: int | None = None,
    reasoning: bool = False,
) -> int:
    """Start (or re-attach to) one agent session.

    ``session_key`` must be DETERMINISTIC for a given workflow and node. Steps
    are at-least-once, so minting a uuid here meant every retry created another
    live session holding another Codex slot, which is exactly the
    externally-visible-step hazard ADR 038 decision 2 calls out.
    """
    from agent_sessions.api import start_session_for_swarm

    return start_session_for_swarm(
        session_key,
        prompt,
        model,
        repo,
        branch,
        workflow_id=workflow_id,
        node_key=node_key,
        node_attempt=node_attempt,
        reasoning=reasoning,
    )


@DBOS.step()
def poll_turn(session_id: int, after_seq: int) -> dict | None:
    """One non-blocking look for the next turn of a session.

    Deliberately does NOT sleep or block. The waiting is done by the caller at
    workflow level with DBOS.sleep, so each wait is checkpointed and a process
    restart resumes the wait instead of losing it. A step that blocked for the
    full turn timeout would hold a worker thread for up to 30 minutes and would
    restart its wait from zero on recovery, which is the fragile-edge shape ADR
    038 decision 1 exists to remove.
    """
    from sqlmodel import Session, select

    from agent_sessions.models import AgentTurn
    from core.db import get_engine
    from agent_sessions.rationale import parse_rationale

    with Session(get_engine()) as session:
        turn = session.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == session_id, AgentTurn.seq > after_seq)
            .order_by(AgentTurn.seq)
        ).first()
        if turn is None:
            return None
        return {
            "seq": turn.seq,
            "prompt_intent": turn.prompt_intent,
            "result_text": turn.result_text,
            "rationale": parse_rationale(turn.result_text),
            "terminal_reason": turn.terminal_reason,
            "cost_usd": turn.cost_usd,
        }


@DBOS.step()
def poll_session_status(session_id: int) -> str:
    """Read the durable lifecycle state paired with ``poll_turn``."""
    from sqlmodel import Session

    from agent_sessions import store
    from core.db import get_engine

    with Session(get_engine()) as session:
        row = store.get_session(session, session_id)
        if row is None:
            return "missing"
        return row.status


@DBOS.step()
def update_turn_shas(
    session_id: int, turn_seq: int, base_sha: str | None, commit_sha: str | None
) -> bool:
    """Attach the swarm's pre- and post-attempt heads to a persisted turn."""
    from sqlmodel import Session

    from agent_sessions import store
    from core.db import get_engine

    with Session(get_engine()) as session:
        return (
            store.update_turn_shas(session, session_id, turn_seq, base_sha, commit_sha)
            is not None
        )


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def read_branch_head(repo: str, branch: str) -> str | None:
    """Read a pushed branch head from GitHub, independently of the agent claim."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["object"]["sha"]
