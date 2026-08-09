from __future__ import annotations

from dbos import DBOS


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def start_agent_session(session_key, prompt, model, repo, branch) -> int:
    """Start (or re-attach to) one agent session.

    ``session_key`` must be DETERMINISTIC for a given workflow and node. Steps
    are at-least-once, so minting a uuid here meant every retry created another
    live session holding another Codex slot, which is exactly the
    externally-visible-step hazard ADR 038 decision 2 calls out.
    """
    from agent_sessions.api import start_session_for_graph

    return start_session_for_graph(session_key, prompt, model, repo, branch)


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
            "result_text": turn.result_text,
            "commit_sha": turn.commit_sha,
            "terminal_reason": turn.terminal_reason,
            "cost_usd": turn.cost_usd,
        }
