from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging

from dbos import DBOS

from agent import config as agent_config
from swarm.workflows import _await_turn

logger = logging.getLogger(__name__)

CLAIM_HOLDER = "qwen-drainer"
CLAIM_TTL_MARGIN_SECONDS = 300
SUMMARY_MAX_CHARS = 2000
CLEAN_TERMINAL_REASONS = {"completed", "end_turn", "stop"}


@DBOS.step()
def pin_drainer_settings() -> dict:
    return asdict(agent_config.load_drainer_settings())


@DBOS.step()
def claim_drainer_job(ttl_secs: int, kind: str) -> dict | None:
    from agent.routine_jobs import claim_job

    return claim_job(holder=CLAIM_HOLDER, ttl_secs=ttl_secs, kind=kind)


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def start_drainer_session(
    local_session_id: str,
    prompt: str,
    repo: str,
    branch: str,
    workflow_id: str,
    reasoning: bool,
) -> int:
    from agent_sessions.api import start_session_for_swarm

    return start_session_for_swarm(
        local_session_id,
        prompt,
        "qwen",
        repo,
        branch,
        workflow_id=workflow_id,
        node_key="qwen-drain",
        reasoning=reasoning,
    )


@DBOS.step()
def finish_drainer_job(name: str, status: str, summary: str) -> bool:
    from agent.routine_jobs import complete_job

    return complete_job(name, status=status, summary=summary)


@DBOS.step()
def notify_drainer_failure(name: str, error: str) -> None:
    from agent.notify import notify

    asyncio.run(notify(f"qwen drainer job {name} failed: {error}", level="warn"))


@DBOS.step()
def destroy_drainer_session(session_id: int | None, local_session_id: str) -> bool:
    from agent_sessions import store
    from agent_sessions.mcp import (
        _clear_ember_bindings_for,
        _load_session_row,
        _transport,
    )
    from agent_sessions.models import PendingMessage
    from agent_sessions.transport import EmberSessionGone
    from core.db import get_engine
    from sqlalchemy import delete
    from sqlmodel import Session

    try:
        if session_id is None:
            with Session(get_engine()) as session:
                row = store.get_session_by_local_id(session, local_session_id)
        else:
            row = _load_session_row(session_id)
    except Exception:  # noqa: BLE001 - cleanup failure must not stop the cycle
        logger.warning(
            "qwen drainer failed to load session %s (%s) for cleanup",
            session_id,
            local_session_id,
            exc_info=True,
        )
        return False
    if row is None or row.id is None:
        return False
    resolved_session_id = row.id
    try:
        # A session that timed out before the orphan sweep claimed its first
        # message must not create a VM after cleanup has already run.
        with Session(get_engine()) as session:
            session.execute(
                delete(PendingMessage).where(
                    PendingMessage.session_id == resolved_session_id
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - still attempt the VM cleanup below
        logger.warning(
            "qwen drainer failed to clear pending turn for session %s",
            resolved_session_id,
            exc_info=True,
        )
    if row.ember_session_id is None:
        return False
    ember_session_id = row.ember_session_id
    try:
        try:
            asyncio.run(_transport.destroy_session(ember_session_id))
        except EmberSessionGone:
            pass
        _clear_ember_bindings_for(ember_session_id)
        return True
    except Exception:  # noqa: BLE001 - cleanup failure must not strand the queue
        logger.warning(
            "qwen drainer failed to destroy session %s (ember %s)",
            resolved_session_id,
            ember_session_id,
            exc_info=True,
        )
        return False


def _workflow_id() -> str:
    try:
        workflow_id = DBOS.workflow_id
    except Exception as exc:  # noqa: BLE001 - DBOS owns the context type
        raise RuntimeError("DBOS workflow id is unavailable") from exc
    if not workflow_id:
        raise RuntimeError("DBOS workflow id is unavailable")
    return workflow_id


def _session_key(workflow_id: str, job_name: str) -> str:
    return f"{workflow_id}:qwen-drain:{job_name}"


def _payload_values(payload: object, settings: dict) -> tuple[str, str, str, bool]:
    if not isinstance(payload, dict):
        raise ValueError("missing usable prompt in payload")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("missing usable prompt in payload")
    repo = payload.get("repo", settings["repo"])
    branch = payload.get("branch", settings["branch"])
    reasoning = payload.get("reasoning", False)
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-empty string")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch must be a non-empty string")
    if not isinstance(reasoning, bool):
        raise ValueError("reasoning must be a boolean")
    return prompt.strip(), repo.strip(), branch.strip(), reasoning


def _summary(value: object) -> str:
    return str(value or "")[:SUMMARY_MAX_CHARS]


def _completed_output(turn: dict) -> str:
    output = _summary(turn.get("result_text"))
    terminal_reason = turn.get("terminal_reason")
    if terminal_reason not in CLEAN_TERMINAL_REASONS:
        raise RuntimeError(
            output or f"turn ended with {terminal_reason or 'no terminal reason'}"
        )
    return output


@DBOS.workflow()
def drain_cycle() -> dict:
    if not agent_config.drainer_enabled():
        return {"status": "disabled", "processed": 0}

    settings = pin_drainer_settings()
    workflow_id = _workflow_id()
    processed = 0
    ttl_secs = settings["turn_timeout_seconds"] + CLAIM_TTL_MARGIN_SECONDS

    for _ in range(settings["max_jobs_per_cycle"]):
        job = claim_drainer_job(ttl_secs, settings["job_kind"])
        if job is None:
            break
        processed += 1
        name = job["name"]

        session_id = None
        local_session_id = _session_key(workflow_id, name)
        start_attempted = False
        try:
            prompt, repo, branch, reasoning = _payload_values(
                job.get("payload"), settings
            )
            start_attempted = True
            session_id = start_drainer_session(
                local_session_id,
                prompt,
                repo,
                branch,
                workflow_id,
                reasoning,
            )
            turn = _await_turn(session_id, 0, settings["turn_timeout_seconds"])
            if turn is None:
                raise TimeoutError(
                    f"turn timed out after {settings['turn_timeout_seconds']} seconds"
                )
            finish_drainer_job(name, "ok", _completed_output(turn))
        except Exception as exc:  # noqa: BLE001 - one failed job must not stop the cycle
            error = _summary(exc)
            finish_drainer_job(name, "error", error)
            if error == "missing usable prompt in payload":
                continue
            try:
                notify_drainer_failure(name, error)
            except Exception:  # noqa: BLE001 - notification is best effort
                logger.warning(
                    "qwen drainer failure notification failed for job %s",
                    name,
                    exc_info=True,
                )
        finally:
            if start_attempted:
                destroy_drainer_session(session_id, local_session_id)

    return {"status": "complete", "processed": processed}
