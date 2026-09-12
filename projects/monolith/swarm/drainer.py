from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import logging
import re

from dbos import DBOS
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

from agent import config as agent_config
from agent_sessions.constants import (
    CLEAN_TERMINAL_REASONS,
    DRAINER_NODE_KEY,
    INTERRUPTED_TERMINAL_REASONS,
    KG_NODE_KEY,
    UNKNOWN_INVOCATION,
    UNKNOWN_INVOCATION_MESSAGE,
)
from knowledge.api import (
    ExtractionOutputInvalid,
    KG_JOB_KIND,
    MAX_GARDENER_RETRIES,
    set_kg_swept_last_cycle,
)
from knowledge.docfix import (
    find_reviewable_docfix_prs,
    prune_completed_docfix_reviews,
    schedule_docfix_review,
)
from swarm.steps import send_agent_session_message, start_agent_session
from swarm.tracing import set_attributes, tracer

logger = logging.getLogger(__name__)

CLAIM_HOLDER = "luna-drainer"
CLAIM_TTL_MARGIN_SECONDS = 300
IDLE_POLL_SECONDS = 5
IDLE_POLL_LIMIT = 180
SPAN_SUMMARY_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 2000
DOCFIX_PR_URL_RE = re.compile(r"github\.com/jomcgi-org/homelab/pull/(\d+)")
DOCFIX_REVIEW_SUMMARY_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)


@DBOS.step()
def _quota_span_attributes() -> dict:
    try:  # nosemgrep: no-broad-except-swallow - optional telemetry only
        from agent_sessions.provider_quota import fetch_provider_quota_sync, summarise

        fetched = fetch_provider_quota_sync()
        if not fetched.get("available", False):
            return {}
        quotas = summarise(fetched.get("providers", {}))
        attributes = {}
        for provider in ("codex", "claude"):
            quota = quotas.get(provider)
            if quota is None:
                continue
            used_percent = quota.get("headline_used_percent")
            if used_percent is not None:
                attributes[f"drain.quota.{provider}.used_percent"] = used_percent
            age_seconds = quota.get("age_seconds")
            if age_seconds is not None:
                attributes[f"drain.quota.{provider}.age_seconds"] = age_seconds
            window = quota.get("headline_window")
            if window is not None:
                attributes[f"drain.quota.{provider}.window"] = window
            attributes[f"drain.quota.{provider}.exhausted"] = quota["exhausted"]
        return attributes
    except Exception:  # noqa: BLE001
        logger.debug("drain quota telemetry failed", exc_info=True)
        return {}


class MalformedPayload(ValueError):
    """A routine job payload failed validation before session creation."""


class InvocationOutcomeUnknown(RuntimeError):
    """Partial output cannot authorize applying results or another attempt."""


@DBOS.step()
def hold_drainer_job(
    name: str,
    session_id: int,
    *,
    expected_holder: str | None = None,
    expected_locked_at=None,
) -> bool:
    from agent.routine_jobs import hold_job_for_unknown_outcome

    guard = {}
    if expected_holder is not None:
        if expected_locked_at is None:
            raise RuntimeError("routine hold lacks original lock timestamp")
        guard = {
            "expected_locked_by": expected_holder,
            "expected_locked_at": expected_locked_at,
        }
    if not hold_job_for_unknown_outcome(
        name, session_id, UNKNOWN_INVOCATION_MESSAGE, **guard
    ):
        raise RuntimeError(
            f"Could not hold routine job {name} for session {session_id}"
        )
    return True


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
                "drain.docfix_auto_merge": settings.get("docfix_auto_merge", False),
                "drain.docfix_review_enabled": settings.get(
                    "docfix_review_enabled", False
                ),
                "drain.turn_timeout_seconds": settings["turn_timeout_seconds"],
                "drain.reasoning": settings["reasoning"],
            },
        )
        return settings


# Every drain job runs on this model, in both the claim reservation and the
# session it later starts. The two must agree: a reservation whose model
# changed is refused, and a refused reservation never dispatches.
DRAIN_MODEL = "luna"


def provider_walled() -> tuple[bool, str]:
    """Whether the drainer's provider is observably out of quota right now.

    Claiming into a spent subscription costs the lease, the rolling daily
    allowance and a recorded failure, and proves nothing: the turn comes back
    saying the usage limit was hit. Deferring the claim leaves all three
    unspent for the reset.

    Only positive evidence of exhaustion defers. An unobserved, stale, or
    already-reset provider claims exactly as it did before, because
    model_pool.availability owns that contract and the factory routes on the
    same reading.
    """
    try:
        from swarm.model_pool import availability, quota_summary

        ok, reason = availability(DRAIN_MODEL, quota_summary())
        return (not ok), reason
    # nosemgrep: no-broad-except-swallow
    except Exception:  # noqa: BLE001 - an unreadable quota never stops the lane
        logger.debug("drain provider quota unreadable", exc_info=True)
        return False, "unreadable"


@DBOS.step()
def claim_drainer_job(
    ttl_secs: int,
    kinds: tuple[str, ...] | list[str],
    workflow_id: str,
    base_kg_cap: int,
    claim_index: int,
) -> dict | None:
    """Claim a lease only after reserving its future session under the pool lock."""
    from agent.routine_jobs import claim_job
    from agent_sessions.admission import (
        adopt_existing,
        lock_pool,
        reserve_start,
        reserved_routine_jobs,
    )
    from core.db import get_engine
    from knowledge.burst import kg_burst_state
    from sqlalchemy import text
    from sqlmodel import Session

    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "agent_sessions" if sqlite else "agent_sessions.agent_sessions"
    cutoff = (
        "datetime(CURRENT_TIMESTAMP, '-24 hours')"
        if sqlite
        else "now() - interval '24 hours'"
    )
    with (
        tracer.start_as_current_span("drain.claim_job") as span,
        Session(engine) as session,
    ):
        walled, reason = provider_walled()
        if walled:
            set_attributes(span, {"drain.deferred": reason})
            logger.info(
                "drain claim deferred: %s provider is walled (%s)",
                DRAIN_MODEL,
                reason,
            )
            return None
        lock_pool(session)
        adopt_existing(session)
        remaining_kinds = tuple(kinds)
        while remaining_kinds:
            # Keep the pool lock while rolling back a refused job's lease.
            # Another worker cannot spend the same allowance or freshness turn.
            savepoint = session.begin_nested()
            job = claim_job(
                holder=f"{CLAIM_HOLDER}:{workflow_id}:{claim_index}",
                ttl_secs=ttl_secs,
                kinds=remaining_kinds,
                prefer_repo_freshness=True,
                session=session,
                recover_holder=True,
                exclude_names=reserved_routine_jobs(session),
            )
            if job is None:
                savepoint.rollback()
                session.commit()
                return None
            is_kg = job["routine_kind"] == KG_JOB_KIND
            node_key = KG_NODE_KEY if is_kg else DRAINER_NODE_KEY
            local_id = _session_key(workflow_id, job["name"], node_key)
            daily = {}
            if is_kg:
                used = int(
                    session.execute(
                        text(
                            f"SELECT count(*) FROM {table} WHERE node_key = :node_key "
                            f"AND created_at >= {cutoff}"
                        ),
                        {"node_key": KG_NODE_KEY},
                    ).scalar_one()
                )
                burst = kg_burst_state(session)
                cap = base_kg_cap
                if burst.active:
                    # Every unbound start spends both the rolling allowance and
                    # the usable grant remainder, including parallel claimers.
                    cap = min(
                        base_kg_cap + burst.extra_jobs, used + burst.remaining_jobs
                    )
                daily = {
                    "daily_key": "kg-rolling-24h",
                    "daily_limit": cap,
                    "daily_used": used,
                }
            admitted = reserve_start(
                session,
                local_id,
                tier="kg" if is_kg else "project",
                model=DRAIN_MODEL,
                routine_job_name=job["name"],
                **daily,
            )
            if admitted:
                savepoint.commit()
                session.commit()
                set_attributes(
                    span,
                    {
                        "drain.job_kinds": ",".join(kinds),
                        "drain.ttl_seconds": ttl_secs,
                        "drain.claimed": True,
                        "drain.job_name": job["name"],
                    },
                )
                return job
            savepoint.rollback()
            if not is_kg:
                session.commit()
                return None
            # A full KG lane or daily limit must not hide ordinary project work.
            remaining_kinds = tuple(
                kind for kind in remaining_kinds if kind != KG_JOB_KIND
            )
        session.commit()
        return None


@DBOS.step()
def drainer_wait_enabled() -> bool:
    """Every routine claim must see current pause settings rather than its old pin."""
    settings = agent_config.load_drainer_settings()
    return settings.enabled and settings.max_jobs_per_cycle > 0


def _claim_with_idle_wait(
    ttl_secs, kinds, workflow_id, base_kg_cap, claim_index, *, wait_allowed
):
    for poll in range(IDLE_POLL_LIMIT + 1):
        if not drainer_wait_enabled():
            return None, False
        job = claim_drainer_job(ttl_secs, kinds, workflow_id, base_kg_cap, claim_index)
        if job is not None:
            return job, False
        if not wait_allowed or IDLE_POLL_LIMIT <= 0 or not drainer_wait_enabled():
            return None, False
        if poll == IDLE_POLL_LIMIT:
            # Rotate this same worker slot every fifteen idle minutes. This
            # bounds checkpoint history without creating rapid no-op workflows.
            return None, True
        DBOS.sleep(IDLE_POLL_SECONDS)
    raise AssertionError("unreachable idle poll state")


@DBOS.step()
def cancel_drainer_reservation(local_session_id: str) -> bool:
    """Refund only a never-created session after local prompt validation fails."""
    from agent_sessions.admission import cancel_unbound
    from core.db import get_engine
    from sqlmodel import Session

    with Session(get_engine()) as session:
        cancelled = cancel_unbound(session, local_session_id)
        session.commit()
        return cancelled


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
def kg_effective_cap(base_cap: int) -> int:
    from core.db import get_engine
    from knowledge.burst import kg_effective_cap as _kg_effective_cap
    from sqlmodel import Session

    with Session(get_engine()) as session:
        return _kg_effective_cap(session, base_cap)


@DBOS.step()
def sweep_kg_raws(limit: int = 50) -> int:
    from core.db import get_engine
    from knowledge.api import sweep_unqueued_raws
    from sqlmodel import Session

    with Session(get_engine()) as session:
        swept = sweep_unqueued_raws(session, limit)
        try:
            prune_completed_docfix_reviews(session)
            pr_numbers = find_reviewable_docfix_prs(session)
            if pr_numbers:
                schedule_docfix_review(session, pr_numbers=pr_numbers, delay_seconds=0)
        except Exception:  # noqa: BLE001 - review sweep must not stop extraction
            logger.warning("docfix-review sweep failed", exc_info=True)
        return swept


@DBOS.step()
def schedule_docfix_review_for_completion(result_text: str) -> bool:
    """Queue a delayed review when a successful docfix turn returns a PR URL."""
    from core.db import get_engine
    from sqlmodel import Session

    match = DOCFIX_PR_URL_RE.search(result_text)
    if match is None:
        return False
    with Session(get_engine()) as session:
        return schedule_docfix_review(
            session, pr_numbers=[int(match.group(1))], delay_seconds=600
        )


@DBOS.step()
def defer_drainer_job(
    name: str, seconds: int, *, expected_holder: str | None = None
) -> bool:
    from agent.routine_jobs import defer_job

    return defer_job(
        name,
        seconds,
        **({"expected_holder": expected_holder} if expected_holder is not None else {}),
    )


@DBOS.step()
def update_drainer_job_payload(
    name: str, payload: dict, *, expected_holder: str | None = None
) -> bool:
    from agent.routine_jobs import update_job_payload

    return update_job_payload(
        name,
        payload,
        **({"expected_holder": expected_holder} if expected_holder is not None else {}),
    )


@DBOS.step()
def increment_kg_job_attempt(name: str, *, expected_holder: str | None = None) -> int:
    """Increment attempts on the current persisted payload and return the count."""
    from agent.routine_jobs import lock_claim
    from core.db import get_engine
    from sqlalchemy import text
    from sqlmodel import Session

    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    payload_expr = ":payload" if sqlite else "CAST(:payload AS JSONB)"
    with Session(engine) as session:
        if not lock_claim(session, name, expected_holder):
            raise RuntimeError("routine job claim ownership changed")
        row = session.execute(
            text(f"SELECT payload FROM {table} WHERE name = :name"), {"name": name}
        ).first()
        if row is None:
            raise MalformedPayload(f"job not found: {name}")
        current = row.payload
        if isinstance(current, str):
            current = json.loads(current)
        payload, attempt = _incremented_kg_payload(current)
        session.execute(
            text(f"UPDATE {table} SET payload = {payload_expr} WHERE name = :name"),
            {"name": name, "payload": json.dumps(payload)},
        )
        session.commit()
    return attempt


@DBOS.step()
def build_kg_prompt(payload: dict) -> str:
    from core.db import get_engine
    from knowledge.api import build_extraction_prompt, build_repo_diff_prompt
    from knowledge.models import RawInput
    from sqlmodel import Session, select

    if payload.get("mode") == "repo-diff":
        return build_repo_diff_prompt(payload.get("last_sha"))
    raw_id = _kg_raw_id(payload)
    with Session(get_engine()) as session:
        raw = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
        if raw is None:
            raise MalformedPayload(f"raw not found: {raw_id}")
        return build_extraction_prompt(session, raw)


@DBOS.step()
def apply_kg_extraction(
    name: str,
    payload: dict,
    result_text: str,
    correction: bool = False,
    *,
    expected_holder: str | None = None,
) -> dict:
    from agent.routine_jobs import lock_claim
    from core.db import get_engine
    from knowledge.api import apply_extraction, apply_repo_diff
    from sqlmodel import Session

    def check_claim(session: Session) -> None:
        if not lock_claim(session, name, expected_holder):
            raise RuntimeError("routine job claim ownership changed")

    with Session(get_engine()) as session:
        check_claim(session)
        if payload.get("mode") == "repo-diff":
            return apply_repo_diff(session, name, result_text)
        raw_id = _kg_raw_id(payload)
        return apply_extraction(
            session,
            raw_id,
            result_text,
            correction=correction,
            transaction_guard=check_claim if expected_holder is not None else None,
        )


@DBOS.step()
def build_kg_correction_prompt(rejected: list[dict]) -> str:
    from knowledge.api import render_correction_prompt

    return render_correction_prompt(rejected)


@DBOS.step()
def record_kg_failure(
    raw_id: str,
    error: str,
    attempt: int,
    *,
    name: str | None = None,
    expected_holder: str | None = None,
) -> None:
    from agent.routine_jobs import lock_claim
    from core.db import get_engine
    from knowledge.api import record_extraction_failure
    from sqlmodel import Session

    with Session(get_engine()) as session:
        if expected_holder is not None and (
            name is None or not lock_claim(session, name, expected_holder)
        ):
            raise RuntimeError("routine job claim ownership changed")
        record_extraction_failure(session, raw_id, error, attempt)


@DBOS.step()
def finish_drainer_job(
    name: str,
    status: str,
    summary: str,
    deregister: bool = False,
    *,
    expected_holder: str | None = None,
    defer_seconds: int | None = None,
) -> bool:
    from agent.routine_jobs import complete_job, deregister_job

    # This span is the countable per-job outcome event. The outcome belongs on
    # finish_job, not on the replayable job span.
    with tracer.start_as_current_span("drain.finish_job") as span:
        if expected_holder is not None:
            completed = complete_job(
                name,
                status=status,
                summary=summary,
                expected_holder=expected_holder,
                deregister=deregister,
                preserve_repo_freshness=True,
                defer_seconds=defer_seconds,
            )
            if not completed:
                raise RuntimeError("routine job claim ownership changed")
        else:
            completed = complete_job(name, status=status, summary=summary)
        if deregister and completed and expected_holder is None:
            # Keep the completed freshness row's cooldown through one-shot
            # cleanup, including final failure before extraction provenance.
            deregister_job(name, preserve_repo_freshness=True)
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


def _report_drainer_failure(settings: dict, name: str, error: str) -> None:
    # settings.get keeps recovery compatible with workflows pinned before this
    # setting existed. The new default is quiet Discord plus a warning log.
    if not settings.get("notify_failures", False):
        logger.warning("Luna drainer job %s failed: %s", name, error)
        return
    try:
        notify_drainer_failure(name, error)
    except Exception:  # noqa: BLE001 - notification is best effort
        logger.warning(
            "Luna drainer failure notification failed for job %s",
            name,
            exc_info=True,
        )


@DBOS.step()
def destroy_drainer_session(session_id: int | None, local_session_id: str) -> bool:
    from agent_sessions import admission, result_receipts, store
    from agent_sessions.execution_api import destroy_and_confirm
    from agent_sessions.mcp import _load_session_row
    from agent_sessions.models import PendingMessage
    from agent_sessions.transport import EmberSessionGone
    from core.db import get_engine
    from sqlalchemy import delete
    from sqlmodel import Session, select

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
            with Session(get_engine()) as session:
                admission.cancel_unbound(session, local_session_id)
                session.commit()
            span.set_attribute("drain.destroyed", False)
            return False
        resolved_session_id = row.id
        try:
            # A session that timed out before the orphan sweep claimed its first
            # message must not create a VM after cleanup has already run.
            current_ember_id = None
            workflow_id = None
            cleanup_claim_id = None
            with Session(get_engine()) as session:
                current = store._lock_session(session, resolved_session_id)
                if current is None or store.has_unknown_outcome(
                    session, resolved_session_id
                ):
                    span.set_attribute("drain.destroyed", False)
                    return False
                current_ember_id = current.ember_session_id
                workflow_id = current.workflow_id
                if current_ember_id is not None:
                    if not workflow_id:
                        # A legacy row without authoritative workflow identity
                        # cannot safely claim this guest.
                        span.set_attribute("drain.destroyed", False)
                        return False
                    # A turn that completed through a result receipt leaves a
                    # fence only the original POST's own validated response
                    # clears, and begin_guest_cleanup refuses while any bound
                    # row carries one, so a lost response used to strand this
                    # cleanup on observer_pending for every later cycle and the
                    # guest ran on to idle_ttl (#6050). Release it here, inside
                    # this transaction: a refused or failed cleanup rolls the
                    # release back with everything else this block was going to
                    # write, and a committed claim blocks dispatch in its place.
                    if current.result_receipt_fence_id is not None:
                        result_receipts.release_abandoned_fence_locked(
                            session, current, current.result_receipt_fence_id
                        )
                    claim = store.begin_guest_cleanup(
                        session,
                        resolved_session_id,
                        current_ember_id,
                        workflow_id,
                    )
                    if claim.get("hold") is not None:
                        # invocation_pending on this row's own attempted
                        # dispatch is only raised once result receipts are
                        # enabled: the guest may still deliver a native result
                        # that receipt-first adoption settles, so the claim
                        # waits and the stranded-claim retry below returns to
                        # it each cycle rather than deleting a live turn.
                        span.set_attribute("drain.destroyed", False)
                        return False
                    cleanup_claim_id = claim["claim_id"]
                    # begin_guest_cleanup commits its claim. Re-lock and read
                    # the row again before applying destructive cleanup.
                    current = store._lock_session(session, resolved_session_id)
                    if (
                        current is None
                        or current.ember_session_id != current_ember_id
                        or current.workflow_id != workflow_id
                        or current.guest_cleanup_id != cleanup_claim_id
                    ):
                        span.set_attribute("drain.destroyed", False)
                        return False
                for pending in session.exec(
                    select(PendingMessage).where(
                        PendingMessage.session_id == resolved_session_id
                    )
                ).all():
                    admission.cancel_unattempted(session, current, pending)
                session.execute(
                    delete(PendingMessage).where(
                        PendingMessage.session_id == resolved_session_id
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001 - failed hold checks must retain the guest
            logger.warning(
                "Luna drainer failed to clear pending turn for session %s",
                resolved_session_id,
                exc_info=True,
            )
            span.set_attribute("drain.destroyed", False)
            return False
        if current_ember_id is None:
            span.set_attribute("drain.destroyed", False)
            return False
        ember_session_id = current_ember_id
        try:
            # Ember answers 202 destroying before teardown completes, and this
            # job is the only caller for its row, so spend a bounded number of
            # confirming reads here. An unconfirmed guest keeps its claim and
            # the next drain cycle retries it (retry_stranded_drainer_cleanups).
            try:
                confirmed = asyncio.run(
                    destroy_and_confirm(
                        ember_session_id,
                        attempts=CLEANUP_CONFIRM_ATTEMPTS,
                        interval_seconds=CLEANUP_CONFIRM_INTERVAL_SECONDS,
                    )
                )
            except EmberSessionGone:
                confirmed = True
            if not confirmed:
                span.set_attribute("drain.destroyed", False)
                return False
            # finish_guest_cleanup retires only the claiming row. Alias rows
            # bound to the same guest self-heal through EmberSessionGone on
            # their next use, matching the workflow reaper's per-row model.
            with Session(get_engine()) as session:
                finished = store.finish_guest_cleanup(
                    session,
                    resolved_session_id,
                    ember_session_id,
                    workflow_id,
                    cleanup_claim_id,
                )
            span.set_attribute("drain.destroyed", finished)
            return finished
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


# Confirming reads after DELETE: five reads two seconds apart covers the
# ordinary asynchronous teardown without holding a drain cycle for long.
CLEANUP_CONFIRM_ATTEMPTS = 5
CLEANUP_CONFIRM_INTERVAL_SECONDS = 2.0
STRANDED_CLEANUP_LIMIT = 8


@DBOS.step()
def stranded_drainer_cleanups(limit: int = STRANDED_CLEANUP_LIMIT) -> list[dict]:
    """List drainer-owned sessions still holding a guest cleanup claim.

    A drainer job calls destroy_drainer_session exactly once. When the guest
    is not terminal by the last confirming read, or the DELETE fails, the
    claim is retained on purpose and no other owner reaps drainer rows: the
    stale-cycle reaper lists PENDING workflows only and the factory
    reconciler owns factory rows. This is the retry owner.
    """
    from agent_sessions.models import AgentSession
    from core.db import get_engine
    from sqlmodel import Session, or_, select

    patterns = [f"%:{key}:%" for key in (DRAINER_NODE_KEY, KG_NODE_KEY)]
    with Session(get_engine()) as session:
        rows = session.exec(
            select(AgentSession.id, AgentSession.local_session_id)
            .where(
                AgentSession.guest_cleanup_id.is_not(None),
                or_(*(AgentSession.local_session_id.like(p) for p in patterns)),
            )
            .order_by(AgentSession.guest_cleanup_started_at, AgentSession.id)
            .limit(limit)
        ).all()
    return [{"session_id": sid, "local_session_id": local} for sid, local in rows]


def retry_stranded_drainer_cleanups(*, list_fn=None, destroy_fn=None) -> dict:
    """Resume every stranded drainer cleanup claim once per cycle."""
    list_fn = stranded_drainer_cleanups if list_fn is None else list_fn
    destroy_fn = destroy_drainer_session if destroy_fn is None else destroy_fn
    stranded = list_fn()
    retired = 0
    for item in stranded:
        if destroy_fn(item["session_id"], item["local_session_id"]):
            retired += 1
    return {"stranded": len(stranded), "retired": retired}


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


def _is_repo_diff(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("mode") == "repo-diff"


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


def _docfix_review_summary(result_text: str) -> str:
    """Keep the reviewer's final machine-readable outcome in the job summary."""
    required = {
        "reviewed",
        "queued",
        "verified",
        "needs_human",
        "skipped_pending",
    }
    for match in reversed(DOCFIX_REVIEW_SUMMARY_RE.findall(result_text)):
        try:
            candidate = json.loads(match)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and required.issubset(candidate):
            return json.dumps(
                {
                    key: candidate[key]
                    for key in (
                        "reviewed",
                        "queued",
                        "verified",
                        "needs_human",
                        "skipped_pending",
                    )
                },
                separators=(",", ":"),
            )
    return _summary(result_text)


def _retry_or_dead_letter_kg(
    name: str,
    raw_id: str | None,
    error: str,
    *,
    recurring: bool,
    expected_holder: str | None = None,
) -> None:
    ownership = (
        {"expected_holder": expected_holder} if expected_holder is not None else {}
    )
    attempt = increment_kg_job_attempt(name, **ownership)
    if attempt < MAX_GARDENER_RETRIES:
        defer_drainer_job(name, 900 * attempt, **ownership)
    else:
        if raw_id is not None:
            record_kg_failure(
                raw_id,
                error,
                attempt,
                **({"name": name, **ownership} if ownership else {}),
            )
        finish_drainer_job(name, "error", error, not recurring, **ownership)


_turn_has_unknown_outcome_lookup = None


def _completed_output(turn: dict, session_id: int | None = None) -> str:
    if turn.get("stop_reason") == UNKNOWN_INVOCATION or _turn_has_unknown_outcome(
        turn, session_id
    ):
        raise InvocationOutcomeUnknown(UNKNOWN_INVOCATION_MESSAGE)
    output = _summary(turn.get("result_text"))
    terminal_reason = turn.get("terminal_reason")
    if terminal_reason in INTERRUPTED_TERMINAL_REASONS:
        raise RuntimeError("interrupted turn cannot be completed")
    if terminal_reason not in CLEAN_TERMINAL_REASONS:
        raise RuntimeError(
            output or f"turn ended with {terminal_reason or 'no terminal reason'}"
        )
    return output


def _turn_has_unknown_outcome(turn: dict, session_id: int | None = None) -> bool:
    if session_id is None or turn.get("seq") is None:
        return False
    try:
        seq = int(turn["seq"])
        # Test seam; production always reads the exact durable admission owner.
        if _turn_has_unknown_outcome_lookup is not None:
            return bool(_turn_has_unknown_outcome_lookup(session_id, seq))
        from agent_sessions import store
        from core.db import get_engine
        from sqlmodel import Session

        with Session(get_engine()) as session:
            return store.has_unknown_outcome_for_turn(session, session_id, seq)
    except Exception as exc:
        # A failed observation cannot authorize retry, dead-letter or cleanup.
        raise InvocationOutcomeUnknown(
            "Could not verify the invocation outcome; retain this attempt"
        ) from exc


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def prepare_next_cycle(workflow_id: str) -> list[str]:
    from swarm.queues import prepare_drainer_workers

    return prepare_drainer_workers(DBOS, completing_workflow_id=workflow_id)


def chain_next_cycle() -> None:
    """Record the refill ids before starting children, so replay cannot shift ids."""
    from swarm.queues import drainer_queue, enqueue_drainer_workers

    work_ids = prepare_next_cycle(_workflow_id())
    enqueue_drainer_workers(DBOS, work_ids, queue=drainer_queue())


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
        try:
            stranded = retry_stranded_drainer_cleanups()
        except Exception:  # noqa: BLE001 - cleanup retry must not stop the cycle
            logger.warning("Luna drainer stranded cleanup retry failed", exc_info=True)
            stranded = {"stranded": -1, "retired": 0}
        set_attributes(
            span,
            {
                "drain.stranded_cleanups": stranded["stranded"],
                "drain.stranded_cleanups_retired": stranded["retired"],
                "drain.workflow_id": workflow_id,
                "drain.job_kinds": ",".join(enabled_kinds),
                "drain.max_jobs_per_cycle": settings["max_jobs_per_cycle"],
            },
        )
        processed = 0
        succeeded = 0
        ttl_secs = settings["turn_timeout_seconds"] + CLAIM_TTL_MARGIN_SECONDS
        claim_kinds = list(enabled_kinds)
        idle_rotation = False

        for claim_index in range(settings["max_jobs_per_cycle"]):
            if not claim_kinds:
                break
            job, idle_rotation = _claim_with_idle_wait(
                ttl_secs,
                tuple(claim_kinds),
                workflow_id,
                settings.get("kg_max_jobs_per_day", 40),
                claim_index,
                wait_allowed=processed == 0 or succeeded > 0,
            )
            if job is None:
                break
            with tracer.start_as_current_span("drain.job") as job_span:
                name = job["name"]
                job_kind = job["routine_kind"]
                ownership = (
                    {"expected_holder": job["locked_by"]}
                    if job.get("locked_by")
                    else {}
                )
                set_attributes(
                    job_span, {"drain.job_name": name, "drain.job_kind": job_kind}
                )

                base_kg_cap = settings.get("kg_max_jobs_per_day", 40)
                if job_kind == KG_JOB_KIND and kg_jobs_today() >= kg_effective_cap(
                    base_kg_cap
                ):
                    cancel_drainer_reservation(
                        _session_key(workflow_id, name, KG_NODE_KEY)
                    )
                    if ownership:
                        finish_drainer_job(
                            name,
                            "deferred",
                            "kg daily cap reached",
                            defer_seconds=3600,
                            **ownership,
                        )
                    else:
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
                outcome_unknown = False
                raw_id = None
                recurring = job.get("interval_secs") is not None
                try:
                    if job_kind == KG_JOB_KIND:
                        job_payload = job.get("payload")
                        if not isinstance(job_payload, dict):
                            raise MalformedPayload("missing kg payload")
                        if not _is_repo_diff(job_payload):
                            raw_id = _kg_raw_id(job_payload)
                        prompt = build_kg_prompt(job_payload)
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
                    quota_attributes = _quota_span_attributes()
                    if quota_attributes:
                        set_attributes(job_span, quota_attributes)
                    start_attempted = True
                    session_id = start_agent_session(
                        local_session_id,
                        prompt,
                        DRAIN_MODEL,
                        repo,
                        branch,
                        workflow_id,
                        node_key,
                        None,
                        reasoning,
                        admission_tier="kg" if job_kind == KG_JOB_KIND else "project",
                    )
                    set_attributes(job_span, {"drain.session_id": session_id})
                    turn = _await_turn(session_id, 0, settings["turn_timeout_seconds"])
                    if turn is None:
                        raise TimeoutError(
                            "turn timed out after "
                            f"{settings['turn_timeout_seconds']} seconds"
                        )
                    output = _completed_output(turn, session_id)
                    if job_kind == KG_JOB_KIND:
                        result_text = str(turn.get("result_text") or "")
                        applied = apply_kg_extraction(
                            name, job_payload, result_text, **ownership
                        )
                        if _is_repo_diff(job_payload):
                            summary = applied["summary"]
                        else:
                            rejected = list(applied.get("rejected") or [])
                            corrected = 0
                            if (
                                job_payload.get("mode") is None
                                and rejected
                                and len(applied["atoms"]) < 3
                                and not applied.get("replayed", False)
                            ):
                                correction_prompt = build_kg_correction_prompt(rejected)
                                send_agent_session_message(
                                    session_id, correction_prompt
                                )
                                first_seq = int(turn.get("seq") or 0)
                                correction_turn = _await_turn(
                                    session_id,
                                    first_seq,
                                    settings["turn_timeout_seconds"],
                                )
                                if correction_turn is None:
                                    raise TimeoutError(
                                        "correction turn timed out after "
                                        f"{settings['turn_timeout_seconds']} seconds"
                                    )
                                _completed_output(correction_turn, session_id)
                                correction_result = apply_kg_extraction(
                                    name,
                                    job_payload,
                                    str(correction_turn.get("result_text") or ""),
                                    correction=True,
                                    **ownership,
                                )
                                corrected = len(correction_result["atoms"])
                                applied["atoms"].extend(correction_result["atoms"])
                                rejected.extend(correction_result.get("rejected") or [])
                                if correction_result.get("dispute") is not None:
                                    applied["dispute"] = correction_result["dispute"]
                                applied["doc_drift"] += correction_result.get(
                                    "doc_drift", 0
                                )
                                applied["docfix_jobs"] += correction_result.get(
                                    "docfix_jobs", 0
                                )
                            summary = (
                                f"atoms={len(applied['atoms'])} "
                                f"rejected={len(rejected)} "
                                f"corrected={corrected} "
                                f"dispute={applied['dispute']} "
                                f"doc_drift={applied['doc_drift']} "
                                f"docfix_jobs={applied['docfix_jobs']}"
                            )
                    else:
                        result_text = str(turn.get("result_text") or "")
                        summary = (
                            _docfix_review_summary(result_text)
                            if name.startswith("docfix-review:")
                            else output
                        )
                    if job_kind == KG_JOB_KIND:
                        finish_drainer_job(
                            name, "ok", summary, not recurring, **ownership
                        )
                    else:
                        completed = finish_drainer_job(name, "ok", summary, **ownership)
                        if completed and name.startswith("docfix:"):
                            try:
                                schedule_docfix_review_for_completion(result_text)
                            except Exception:  # noqa: BLE001 - review is best effort
                                logger.warning(
                                    "could not schedule review after docfix job %s",
                                    name,
                                    exc_info=True,
                                )
                    succeeded += 1
                except MalformedPayload as exc:
                    error = _summary(exc)
                    if job_kind == KG_JOB_KIND:
                        finish_drainer_job(
                            name, "error", error, not recurring, **ownership
                        )
                    else:
                        finish_drainer_job(name, "error", error, **ownership)
                except InvocationOutcomeUnknown as exc:
                    outcome_unknown = True
                    hold_drainer_job(
                        name,
                        session_id,
                        **(
                            {**ownership, "expected_locked_at": job["locked_at"]}
                            if ownership
                            else {}
                        ),
                    )
                    _report_drainer_failure(settings, name, _summary(exc))
                except ExtractionOutputInvalid as exc:
                    error = _summary(exc)
                    _retry_or_dead_letter_kg(
                        name,
                        raw_id,
                        error,
                        recurring=recurring,
                        **ownership,
                    )
                    _report_drainer_failure(settings, name, error)
                except Exception as exc:  # noqa: BLE001 - one job must not stop the cycle
                    error = _summary(exc)
                    if job_kind == KG_JOB_KIND:
                        _retry_or_dead_letter_kg(
                            name,
                            raw_id,
                            error,
                            recurring=recurring,
                            **ownership,
                        )
                    else:
                        finish_drainer_job(name, "error", error, **ownership)
                    _report_drainer_failure(settings, name, error)
                finally:
                    if not start_attempted:
                        cancel_drainer_reservation(local_session_id)
                    if start_attempted and not outcome_unknown:
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
        # cannot spin. The successor reuses this worker slot, while the other worker
        # may still be processing its own job. DBOS bounds execution to two.
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
        if idle_rotation or (
            processed and succeeded and processed >= settings["max_jobs_per_cycle"]
        ):
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
