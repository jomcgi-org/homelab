from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging

from dbos import DBOS
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

from agent import config as agent_config
from agent_sessions.constants import CLEAN_TERMINAL_REASONS, DRAINER_NODE_KEY
from swarm.steps import start_agent_session
from swarm.tracing import set_attributes, tracer

logger = logging.getLogger(__name__)

CLAIM_HOLDER = "qwen-drainer"
CLAIM_TTL_MARGIN_SECONDS = 300
SPAN_SUMMARY_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 2000


class MalformedPayload(ValueError):
    """A routine job payload failed validation before session creation."""


@DBOS.step()
def pin_drainer_settings() -> dict:
    with tracer.start_as_current_span("drain.pin_settings") as span:
        settings = asdict(agent_config.load_drainer_settings())
        set_attributes(
            span,
            {
                "drain.enabled": settings["enabled"],
                "drain.job_kind": settings["job_kind"],
                "drain.max_jobs_per_cycle": settings["max_jobs_per_cycle"],
                "drain.turn_timeout_seconds": settings["turn_timeout_seconds"],
                "drain.reasoning": settings["reasoning"],
            },
        )
        return settings


@DBOS.step()
def claim_drainer_job(ttl_secs: int, kind: str) -> dict | None:
    from agent.routine_jobs import claim_job

    with tracer.start_as_current_span("drain.claim_job") as span:
        job = claim_job(holder=CLAIM_HOLDER, ttl_secs=ttl_secs, kind=kind)
        set_attributes(
            span,
            {
                "drain.job_kind": kind,
                "drain.ttl_seconds": ttl_secs,
                "drain.claimed": job is not None,
                "drain.job_name": job.get("name") if job is not None else None,
            },
        )
        return job


@DBOS.step()
def finish_drainer_job(name: str, status: str, summary: str) -> bool:
    from agent.routine_jobs import complete_job

    # This span is the countable per-job outcome event. The outcome belongs on
    # finish_job, not on the replayable job span.
    with tracer.start_as_current_span("drain.finish_job") as span:
        completed = complete_job(name, status=status, summary=summary)
        summary_lines = summary.splitlines()
        first_line = summary_lines[0] if summary_lines else ""
        set_attributes(
            span,
            {
                "drain.job_name": name,
                "drain.status": status,
                "drain.completed": completed,
                "drain.summary": first_line[:SPAN_SUMMARY_MAX_CHARS],
            },
        )
        if status != "ok":
            span.set_status(Status(StatusCode.ERROR))
        return completed


@DBOS.step()
def notify_drainer_failure(name: str, error: str) -> None:
    from agent.notify import notify

    with tracer.start_as_current_span("drain.notify_failure") as span:
        set_attributes(span, {"drain.job_name": name})
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

    with tracer.start_as_current_span("drain.destroy_session") as span:
        set_attributes(
            span,
            {
                "drain.session_id": session_id,
                "drain.local_session_id": local_session_id,
            },
        )
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
            span.set_attribute("drain.destroyed", False)
            return False
        if row is None or row.id is None:
            span.set_attribute("drain.destroyed", False)
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
            span.set_attribute("drain.destroyed", False)
            return False
        ember_session_id = row.ember_session_id
        try:
            try:
                asyncio.run(_transport.destroy_session(ember_session_id))
            except EmberSessionGone:
                pass
            _clear_ember_bindings_for(ember_session_id)
            span.set_attribute("drain.destroyed", True)
            return True
        except Exception:  # noqa: BLE001 - cleanup failure must not strand the queue
            logger.warning(
                "qwen drainer failed to destroy session %s (ember %s)",
                resolved_session_id,
                ember_session_id,
                exc_info=True,
            )
            span.set_attribute("drain.destroyed", False)
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
    return f"{workflow_id}:{DRAINER_NODE_KEY}:{job_name}"


def _await_turn(session_id: int, after_seq: int, timeout_seconds: int) -> dict | None:
    from swarm.workflows import _await_turn as await_turn

    return await_turn(session_id, after_seq, timeout_seconds)


def _payload_values(payload: object, settings: dict) -> tuple[str, str, str, bool]:
    if not isinstance(payload, dict):
        raise MalformedPayload("missing usable prompt in payload")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise MalformedPayload("missing usable prompt in payload")
    repo = payload.get("repo", settings["repo"])
    branch = payload.get("branch", settings["branch"])
    # Default from settings, like repo and branch above, so an explicit
    # per-job "reasoning": false still wins over the lane default.
    #
    # settings.get, not settings[...]: pin_drainer_settings is a checkpointed
    # step, so a cycle that was in flight when this deploy landed is recovered
    # afterwards and REPLAYS the settings dict it pinned before the key
    # existed. An eager subscript would raise KeyError for every job that
    # cycle claims, and the per-job handler would finish each as "error",
    # which for a one-shot job is permanent. repo and branch never needed this
    # because they predate the pinning.
    reasoning = payload.get("reasoning", settings.get("reasoning", False))
    if not isinstance(repo, str) or not repo.strip():
        raise MalformedPayload("repo must be a non-empty string")
    if not isinstance(branch, str) or not branch.strip():
        raise MalformedPayload("branch must be a non-empty string")
    if not isinstance(reasoning, bool):
        raise MalformedPayload("reasoning must be a boolean")
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


def chain_next_cycle() -> None:
    """Enqueue the successor cycle, without waiting for it.

    Deliberately does NOT call handle.get_result(). The drainer queue has
    concurrency 1, so a successor cannot start until this workflow finishes;
    blocking on it here would hold the only slot waiting for something that
    cannot run, which is a deadlock rather than a slow path.
    """
    from swarm.queues import drainer_queue

    drainer_queue().enqueue(drain_cycle)


@DBOS.workflow()
def drain_cycle() -> dict:
    # context=Context() forces a root trace so cycles running for tens of
    # minutes do not attach to short-lived enqueue traces.
    with tracer.start_as_current_span("drain.cycle", context=Context()) as span:
        settings = pin_drainer_settings()
        if not settings["enabled"]:
            set_attributes(
                span,
                {
                    "drain.outcome": "disabled",
                    "drain.jobs_claimed": 0,
                    "drain.jobs_succeeded": 0,
                },
            )
            return {"status": "disabled", "processed": 0}

        workflow_id = _workflow_id()
        set_attributes(
            span,
            {
                "drain.workflow_id": workflow_id,
                "drain.job_kind": settings["job_kind"],
                "drain.max_jobs_per_cycle": settings["max_jobs_per_cycle"],
            },
        )
        processed = 0
        succeeded = 0
        ttl_secs = settings["turn_timeout_seconds"] + CLAIM_TTL_MARGIN_SECONDS

        for _ in range(settings["max_jobs_per_cycle"]):
            job = claim_drainer_job(ttl_secs, settings["job_kind"])
            if job is None:
                break
            with tracer.start_as_current_span("drain.job") as job_span:
                processed += 1
                name = job["name"]
                set_attributes(job_span, {"drain.job_name": name})

                session_id = None
                local_session_id = _session_key(workflow_id, name)
                set_attributes(
                    job_span,
                    {"drain.local_session_id": local_session_id},
                )
                # Outcome deliberately belongs on drain.finish_job, the
                # non-replaying step, to avoid double-counting on recovery.
                start_attempted = False
                try:
                    prompt, repo, branch, reasoning = _payload_values(
                        job.get("payload"), settings
                    )
                    set_attributes(
                        job_span,
                        {
                            "drain.repo": repo,
                            "drain.branch": branch,
                            "drain.reasoning": reasoning,
                        },
                    )
                    start_attempted = True
                    session_id = start_agent_session(
                        local_session_id,
                        prompt,
                        "qwen",
                        repo,
                        branch,
                        workflow_id,
                        DRAINER_NODE_KEY,
                        None,
                        reasoning,
                    )
                    set_attributes(job_span, {"drain.session_id": session_id})
                    turn = _await_turn(session_id, 0, settings["turn_timeout_seconds"])
                    if turn is None:
                        raise TimeoutError(
                            "turn timed out after "
                            f"{settings['turn_timeout_seconds']} seconds"
                        )
                    finish_drainer_job(name, "ok", _completed_output(turn))
                    succeeded += 1
                except MalformedPayload as exc:
                    error = _summary(exc)
                    finish_drainer_job(name, "error", error)
                except Exception as exc:  # noqa: BLE001 - one job must not stop the cycle
                    error = _summary(exc)
                    finish_drainer_job(name, "error", error)
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

        # Chain straight into the next cycle when this one stopped because it hit
        # max_jobs_per_cycle rather than because the queue ran dry. Without this a
        # deep backlog drains in bursts: a cycle takes its 15 jobs, exits, and the
        # queue then sits idle until the next */15 tick, so a 45-job backlog spends
        # roughly half an hour doing nothing at cycle boundaries. The bound exists
        # to keep any single workflow short, not to rate limit the lane.
        #
        # Three conditions, each load bearing.
        #
        # processed == the bound means every claim returned a job, so there was
        # more work than one cycle could take. A cycle that stops early (claim
        # returned None) does NOT chain, so an empty queue costs nothing and this
        # cannot spin. Queue concurrency is still 1, so the successor waits for
        # this workflow rather than running alongside it.
        #
        # succeeded > 0 is the circuit breaker. When the downstream is sick (say
        # EmberVM is down) every claimed job fails in seconds, and a failed
        # one-shot is PERMANENTLY done: complete_job NULLs its next_run_at
        # whatever the status. Chaining through that destroys the backlog at
        # several hundred dead jobs an hour, with a Discord warn each. Falling
        # back to the next tick gives a 15 minute backoff exactly when something
        # is wrong, and a batch that is genuinely all-garbage still drains, just
        # at tick pace.
        #
        # processed > 0 guards a bound of zero. DRAINER_MAX_JOBS_PER_CYCLE is
        # unvalidated int(env), so setting it to 0 as a way to pause the lane
        # would otherwise satisfy 0 >= 0 and chain an endless one-per-second
        # no-op, writing unbounded workflow_status rows.
        chained = False
        if processed and succeeded and processed >= settings["max_jobs_per_cycle"]:
            chain_next_cycle()
            chained = True

        set_attributes(
            span,
            {
                "drain.jobs_claimed": processed,
                "drain.jobs_succeeded": succeeded,
                "drain.chained": chained,
                "drain.outcome": (
                    "bound_reached"
                    if processed >= settings["max_jobs_per_cycle"] and processed > 0
                    else "queue_empty"
                ),
            },
        )
        return {"status": "complete", "processed": processed}
