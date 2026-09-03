from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging

from dbos import DBOS
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

from agent import config as agent_config
from agent_sessions.constants import (
    CLEAN_TERMINAL_REASONS,
    DRAINER_NODE_KEY,
    KG_NODE_KEY,
)
from knowledge.api import (
    ExtractionOutputInvalid,
    KG_JOB_KIND,
    MAX_GARDENER_RETRIES,
    set_kg_swept_last_cycle,
)
from swarm.steps import start_agent_session
from swarm.tracing import set_attributes, tracer

logger = logging.getLogger(__name__)

CLAIM_HOLDER = "luna-drainer"
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
                "drain.job_kinds": ",".join(settings["job_kinds"]),
                "drain.max_jobs_per_cycle": settings["max_jobs_per_cycle"],
                "drain.kg_max_jobs_per_day": settings["kg_max_jobs_per_day"],
                "drain.turn_timeout_seconds": settings["turn_timeout_seconds"],
                "drain.reasoning": settings["reasoning"],
            },
        )
        return settings


@DBOS.step()
def claim_drainer_job(ttl_secs: int, kinds: tuple[str, ...] | list[str]) -> dict | None:
    from agent.routine_jobs import claim_job

    with tracer.start_as_current_span("drain.claim_job") as span:
        job = claim_job(holder=CLAIM_HOLDER, ttl_secs=ttl_secs, kinds=kinds)
        set_attributes(
            span,
            {
                "drain.job_kinds": ",".join(kinds),
                "drain.ttl_seconds": ttl_secs,
                "drain.claimed": job is not None,
                "drain.job_name": job.get("name") if job is not None else None,
            },
        )
        return job


@DBOS.step()
def kg_jobs_today() -> int:
    from core.db import get_engine
    from sqlalchemy import text
    from sqlmodel import Session

    with Session(get_engine()) as session:
        return int(
            session.execute(
                text(
                    """
                    SELECT count(*)
                      FROM agent_sessions.agent_sessions
                     WHERE node_key = :node_key
                       AND created_at >= now() - interval '24 hours'
                    """
                ),
                {"node_key": KG_NODE_KEY},
            ).scalar_one()
        )


@DBOS.step()
def sweep_kg_raws(limit: int = 50) -> int:
    from core.db import get_engine
    from knowledge.api import sweep_unqueued_raws
    from sqlmodel import Session

    with Session(get_engine()) as session:
        return sweep_unqueued_raws(session, limit)


@DBOS.step()
def defer_drainer_job(name: str, seconds: int) -> bool:
    from agent.routine_jobs import defer_job

    return defer_job(name, seconds)


@DBOS.step()
def update_drainer_job_payload(name: str, payload: dict) -> bool:
    from agent.routine_jobs import update_job_payload

    return update_job_payload(name, payload)


@DBOS.step()
def build_kg_prompt(raw_id: str) -> str:
    from core.db import get_engine
    from knowledge.api import build_extraction_prompt
    from knowledge.models import RawInput
    from sqlmodel import Session, select

    with Session(get_engine()) as session:
        raw = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
        if raw is None:
            raise MalformedPayload(f"raw not found: {raw_id}")
        return build_extraction_prompt(session, raw)


@DBOS.step()
def apply_kg_extraction(raw_id: str, result_text: str) -> dict:
    from core.db import get_engine
    from knowledge.api import apply_extraction
    from sqlmodel import Session

    with Session(get_engine()) as session:
        return apply_extraction(session, raw_id, result_text)


@DBOS.step()
def record_kg_failure(raw_id: str, error: str, attempt: int) -> None:
    from core.db import get_engine
    from knowledge.api import record_extraction_failure
    from sqlmodel import Session

    with Session(get_engine()) as session:
        record_extraction_failure(session, raw_id, error, attempt)


@DBOS.step()
def finish_drainer_job(
    name: str, status: str, summary: str, deregister: bool = False
) -> bool:
    from agent.routine_jobs import complete_job, deregister_job

    # This span is the countable per-job outcome event. The outcome belongs on
    # finish_job, not on the replayable job span.
    with tracer.start_as_current_span("drain.finish_job") as span:
        completed = complete_job(name, status=status, summary=summary)
        if deregister:
            deregister_job(name)
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
        asyncio.run(notify(f"Luna drainer job {name} failed: {error}", level="warn"))


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
                "Luna drainer failed to load session %s (%s) for cleanup",
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
                "Luna drainer failed to clear pending turn for session %s",
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
                "Luna drainer failed to destroy session %s (ember %s)",
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


def _session_key(
    workflow_id: str, job_name: str, node_key: str = DRAINER_NODE_KEY
) -> str:
    return f"{workflow_id}:{node_key}:{job_name}"


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


def _kg_raw_id(payload: object) -> str:
    if not isinstance(payload, dict):
        raise MalformedPayload("missing raw_id in payload")
    raw_id = payload.get("raw_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise MalformedPayload("missing raw_id in payload")
    return raw_id.strip()


def _job_kinds(settings: dict) -> tuple[str, ...]:
    if "job_kinds" in settings:
        return tuple(settings["job_kinds"])
    return (settings.get("job_kind", "qwen-drain"),)


def _incremented_kg_payload(payload: object) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        raise MalformedPayload("missing raw_id in payload")
    previous = payload.get("attempts", 0)
    if not isinstance(previous, int) or isinstance(previous, bool) or previous < 0:
        previous = 0
    attempt = previous + 1
    updated = dict(payload)
    updated["attempts"] = attempt
    return updated, attempt


def _summary(value: object) -> str:
    return str(value or "")[:SUMMARY_MAX_CHARS]


def _retry_or_dead_letter_kg(
    name: str, raw_id: str, job_payload: object, error: str
) -> None:
    payload, attempt = _incremented_kg_payload(job_payload)
    update_drainer_job_payload(name, payload)
    if attempt < MAX_GARDENER_RETRIES:
        defer_drainer_job(name, 900 * attempt)
    else:
        record_kg_failure(raw_id, error, attempt)
        finish_drainer_job(name, "error", error, True)


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

        enabled_kinds = _job_kinds(settings)
        if KG_JOB_KIND in enabled_kinds:
            set_kg_swept_last_cycle(sweep_kg_raws())

        workflow_id = _workflow_id()
        set_attributes(
            span,
            {
                "drain.workflow_id": workflow_id,
                "drain.job_kinds": ",".join(enabled_kinds),
                "drain.max_jobs_per_cycle": settings["max_jobs_per_cycle"],
            },
        )
        processed = 0
        succeeded = 0
        ttl_secs = settings["turn_timeout_seconds"] + CLAIM_TTL_MARGIN_SECONDS
        claim_kinds = list(enabled_kinds)

        for _ in range(settings["max_jobs_per_cycle"]):
            if not claim_kinds:
                break
            job = claim_drainer_job(ttl_secs, tuple(claim_kinds))
            if job is None:
                break
            with tracer.start_as_current_span("drain.job") as job_span:
                name = job["name"]
                job_kind = job["routine_kind"]
                set_attributes(
                    job_span, {"drain.job_name": name, "drain.job_kind": job_kind}
                )

                if job_kind == KG_JOB_KIND and kg_jobs_today() >= settings.get(
                    "kg_max_jobs_per_day", 40
                ):
                    finish_drainer_job(name, "deferred", "kg daily cap reached")
                    defer_drainer_job(name, 3600)
                    claim_kinds = [kind for kind in claim_kinds if kind != KG_JOB_KIND]
                    continue

                processed += 1

                session_id = None
                node_key = KG_NODE_KEY if job_kind == KG_JOB_KIND else DRAINER_NODE_KEY
                local_session_id = _session_key(workflow_id, name, node_key)
                set_attributes(
                    job_span,
                    {"drain.local_session_id": local_session_id},
                )
                # Outcome deliberately belongs on drain.finish_job, the
                # non-replaying step, to avoid double-counting on recovery.
                start_attempted = False
                try:
                    if job_kind == KG_JOB_KIND:
                        raw_id = _kg_raw_id(job.get("payload"))
                        prompt = build_kg_prompt(raw_id)
                        repo = settings["repo"]
                        branch = settings["branch"]
                        reasoning = settings.get("reasoning", False)
                    else:
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
                        "luna",
                        repo,
                        branch,
                        workflow_id,
                        node_key,
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
                    output = _completed_output(turn)
                    if job_kind == KG_JOB_KIND:
                        result_text = str(turn.get("result_text") or "")
                        applied = apply_kg_extraction(raw_id, result_text)
                        summary = (
                            f"atoms={len(applied['atoms'])} "
                            f"dispute={applied['dispute']}"
                        )
                    else:
                        summary = output
                    if job_kind == KG_JOB_KIND:
                        finish_drainer_job(name, "ok", summary, True)
                    else:
                        finish_drainer_job(name, "ok", summary)
                    succeeded += 1
                except MalformedPayload as exc:
                    error = _summary(exc)
                    if job_kind == KG_JOB_KIND:
                        finish_drainer_job(name, "error", error, True)
                    else:
                        finish_drainer_job(name, "error", error)
                except ExtractionOutputInvalid as exc:
                    error = _summary(exc)
                    _retry_or_dead_letter_kg(name, raw_id, job.get("payload"), error)
                    try:
                        notify_drainer_failure(name, error)
                    except Exception:  # noqa: BLE001 - notification is best effort
                        logger.warning(
                            "Luna drainer failure notification failed for job %s",
                            name,
                            exc_info=True,
                        )
                except Exception as exc:  # noqa: BLE001 - one job must not stop the cycle
                    error = _summary(exc)
                    if job_kind == KG_JOB_KIND:
                        _retry_or_dead_letter_kg(
                            name, raw_id, job.get("payload"), error
                        )
                    else:
                        finish_drainer_job(name, "error", error)
                    try:
                        notify_drainer_failure(name, error)
                    except Exception:  # noqa: BLE001 - notification is best effort
                        logger.warning(
                            "Luna drainer failure notification failed for job %s",
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
