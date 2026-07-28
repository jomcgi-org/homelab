"""Directive autopilot (ADR chat/007, PR 3 of the /improve-ambient program).

A silent background loop that auto-applies high-confidence, low-risk channel and
personal directive refinements from ambient interaction signals, self-validates
against the next window's reactions, and auto-reverts on regression. It NEVER
posts to Discord: its apply / keep / revert provenance is exposed only through
the MCP introspection surface (agent/mcp.py monolith-chat-* tools) for
out-of-band review and manual tuning. A human manual tune always wins via the
source-precedence rule.

Runs as the ``chat-directive-autopilot`` Argo CronWorkflow one-shot
(``app/jobs_main.py``, ``chart/values.yaml`` ``jobs.cronWorkflows``), mirroring
``observer_job``: a sync DB fetch phase (``asyncio.to_thread``, own session), an
async LLM classify phase, then sync apply / log phases (``asyncio.to_thread``,
own session). No ``Session`` ever crosses an ``await`` (monolith async/session
rule; semgrep no-sync-session-in-async-def / no-session-in-to-thread).

Two phases per run:

1. VALIDATE (first): for ``directive_autopilot`` rows status='pending_validation'
   whose ``validate_after`` has passed, recompute the post-apply score over
   [applied_at, now]. If the scope's active row is now source='manual' ->
   superseded_manual. Else if the post score dropped more than
   AUTOPILOT_REGRESS_MARGIN below the stored baseline -> revert (reinstate the
   stored prior text) and mark reverted; otherwise kept.
2. APPLY: gather engage episodes since the lookback, cluster per channel and per
   trigger author, classify each cluster with enough evidence, and apply behind a
   hard gate (confidence, evidence count, tone-only guard, bounded length delta,
   no manual precedence, off cooldown). A gated pass applies silently and
   schedules its own validation; a confident-but-ungated finding is recorded for
   review (channel: a staged proposal; user: a logged suggestion). Guard-blocked
   or low-confidence findings do nothing.

Kill switch: AUTOPILOT_MODE='shadow' does everything EXCEPT mutate directives -
it records what it WOULD apply into the log with status='shadow' and writes
nothing to channel_directive / user_style_pref, and it does not revert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlmodel import Session, select

from chat import ambient_analysis, directives
from chat.models import ChannelDirective, DirectiveAutopilot, UserStylePref

logger = logging.getLogger("monolith.chat.autopilot")

# All knobs are env-overridable (AUTOPILOT_*). Chosen LIVE + AGGRESSIVE per the
# PR 3 task brief (which overrides the plan's conservative defaults).
_DEFAULT_MODE = "live"  # "live" | "shadow" (kill switch)
_DEFAULT_MIN_CONFIDENCE = 0.65
_DEFAULT_MIN_EVIDENCE = 2  # distinct cited episodes
_DEFAULT_COOLDOWN_DAYS = 2  # per scope, between autopilot actions
_DEFAULT_VALIDATE_DAYS = 1  # delay before the keep/revert decision
_DEFAULT_MANUAL_COOLDOWN_DAYS = 30  # a human manual row blocks autopilot this long
_DEFAULT_REGRESS_MARGIN = 0.1  # post must drop more than this below baseline
_DEFAULT_LOOKBACK_DAYS = 7  # analysis window the apply phase gathers over
# Bounded-refinement cap: a proposal whose length differs from the current
# directive by more than this many characters is a rewrite, not a refinement, and
# is not auto-applied (it can still be recorded as a proposal for review).
_DEFAULT_MAX_LEN_DELTA = 300


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _mode() -> str:
    mode = os.environ.get("AUTOPILOT_MODE", _DEFAULT_MODE).strip().lower()
    return mode if mode in ("live", "shadow") else _DEFAULT_MODE


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware. SQLite test fixtures store
    naive datetimes; production (Postgres) is tz-aware. Coercing both sides of a
    subtraction keeps the age math valid under both."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _length_ok(current: str, proposed: str) -> bool:
    """A bounded refinement changes the directive length by at most the cap."""
    cap = _int_env("AUTOPILOT_MAX_LEN_DELTA", _DEFAULT_MAX_LEN_DELTA)
    return abs(len(proposed) - len(current or "")) <= cap


# --- shared session-scoped reads ---------------------------------------------


def _active_channel_row(session: Session, channel_id: str) -> ChannelDirective | None:
    return session.exec(
        select(ChannelDirective)
        .where(ChannelDirective.channel_id == channel_id)
        .where(ChannelDirective.active == True)  # noqa: E712 - SQL boolean
    ).first()


def _active_pref_row(session: Session, user_id: str) -> UserStylePref | None:
    return session.exec(
        select(UserStylePref)
        .where(UserStylePref.user_id == user_id)
        .where(UserStylePref.active == True)  # noqa: E712 - SQL boolean
    ).first()


def _current_detail(
    session: Session, scope_kind: str, scope_id: str
) -> tuple[str, int | None]:
    """The active directive/pref text and version for a scope. Version is None for
    a user pref (UserStylePref is unversioned; a revert keys on the stored text)."""
    if scope_kind == "user":
        row = _active_pref_row(session, scope_id)
        return (row.pref if row is not None else "", None)
    row = _active_channel_row(session, scope_id)
    return (row.directive if row is not None else "", row.version if row else 0)


def _active_source(session: Session, scope_kind: str, scope_id: str) -> str:
    row = (
        _active_pref_row(session, scope_id)
        if scope_kind == "user"
        else _active_channel_row(session, scope_id)
    )
    return row.source if row is not None else ""


def _manual_block(
    session: Session, scope_kind: str, scope_id: str, now: datetime
) -> bool:
    """True if the scope's active row is a human manual tune inside the manual
    cooldown, so the autopilot must leave it alone (source precedence)."""
    row = (
        _active_pref_row(session, scope_id)
        if scope_kind == "user"
        else _active_channel_row(session, scope_id)
    )
    if row is None or row.source != "manual":
        return False
    cooldown = _int_env("AUTOPILOT_MANUAL_COOLDOWN_DAYS", _DEFAULT_MANUAL_COOLDOWN_DAYS)
    return _as_utc(now) - _as_utc(row.created_at) < timedelta(days=cooldown)


def _autopilot_cooldown(
    session: Session, scope_kind: str, scope_id: str, now: datetime
) -> bool:
    """True if the scope saw ANY autopilot action within the per-scope cooldown."""
    row = session.exec(
        select(DirectiveAutopilot)
        .where(DirectiveAutopilot.scope_kind == scope_kind)
        .where(DirectiveAutopilot.scope_id == scope_id)
        .order_by(DirectiveAutopilot.id.desc())
    ).first()
    if row is None:
        return False
    cooldown = _int_env("AUTOPILOT_COOLDOWN_DAYS", _DEFAULT_COOLDOWN_DAYS)
    return _as_utc(now) - _as_utc(row.created_at) < timedelta(days=cooldown)


# --- APPLY phase -------------------------------------------------------------


def _trim_episode(ep: dict) -> dict:
    """The subset of an episode classify_scope needs, as plain data safe to hand
    back across the to_thread boundary."""
    return {
        "episode_id": ep["episode_id"],
        "reaction_valence": ep["reaction_valence"],
        "followup": ep["followup"],
        "barged_in": ep["barged_in"],
        "text": ep.get("text", ""),
    }


def _gather_candidates(since: datetime, now: datetime) -> list[dict]:
    """Sync DB phase: the scopes eligible for classification this run.

    A scope qualifies when it has at least AUTOPILOT_MIN_EVIDENCE engage episodes
    since ``since``, is not under a manual-precedence block, and is off its
    autopilot cooldown. Returns ``{scope_kind, scope_id, episodes, current_directive}``
    per candidate. Opens its own session (runs under ``asyncio.to_thread``).
    """
    from core.db import get_engine

    min_evidence = _int_env("AUTOPILOT_MIN_EVIDENCE", _DEFAULT_MIN_EVIDENCE)
    candidates: list[dict] = []
    with Session(get_engine()) as session:
        clusters = ambient_analysis.gather_scope_episodes(session, since)
        for scope_kind in ("channel", "user"):
            for scope_id, eps in clusters[scope_kind].items():
                if not scope_id or len(eps) < min_evidence:
                    continue
                if _manual_block(session, scope_kind, scope_id, now):
                    continue
                if _autopilot_cooldown(session, scope_kind, scope_id, now):
                    continue
                current_text, _ = _current_detail(session, scope_kind, scope_id)
                candidates.append(
                    {
                        "scope_kind": scope_kind,
                        "scope_id": scope_id,
                        "episodes": [_trim_episode(e) for e in eps],
                        "current_directive": current_text,
                    }
                )
    return candidates


def _write_log(
    session: Session,
    *,
    scope_kind: str,
    scope_id: str,
    target_version: int,
    prior_version: int | None,
    prior_text: str,
    baseline: float | None,
    rationale: str,
    evidence_ids: list[int],
    status: str,
    now: datetime,
    validate_after: datetime,
) -> None:
    session.add(
        DirectiveAutopilot(
            scope_kind=scope_kind,
            scope_id=scope_id,
            target_version=target_version,
            prior_version=prior_version,
            prior_text=prior_text,
            baseline_json=json.dumps({"score": baseline}),
            rationale=rationale,
            evidence_json=json.dumps(evidence_ids),
            status=status,
            applied_at=now,
            validate_after=validate_after,
            created_at=now,
        )
    )


def _apply_or_log(
    candidate: dict, result: dict, since: datetime, now: datetime, mode: str
) -> str:
    """Gate a classified scope and act. Returns the outcome tag (for logging).

    The gate (ALL must hold to APPLY): confidence >= AUTOPILOT_MIN_CONFIDENCE,
    cited evidence >= AUTOPILOT_MIN_EVIDENCE, tone-only guard passes, bounded
    length delta, and (defensively re-checked here) no manual-precedence block.
    Guard-blocked or low-confidence findings do nothing. A confident, guard-safe,
    but ungated finding is recorded for review: channel via a staged proposal
    (no message posted, so still silent) plus a 'proposed' log row; user via a
    'suggested' log row (there is no personal-pref proposal flow). Fail-closed:
    exceptions propagate to the handler, which logs and skips the scope.
    """
    scope_kind = candidate["scope_kind"]
    scope_id = candidate["scope_id"]
    proposed = (result.get("proposed_text") or "").strip()
    confidence = float(result.get("confidence") or 0.0)
    evidence_ids = list(result.get("evidence_ids") or [])
    rationale = result.get("rationale") or ""

    min_conf = _float_env("AUTOPILOT_MIN_CONFIDENCE", _DEFAULT_MIN_CONFIDENCE)
    min_evidence = _int_env("AUTOPILOT_MIN_EVIDENCE", _DEFAULT_MIN_EVIDENCE)

    if confidence < min_conf or not proposed:
        return "abstain"
    if not directives.guard(proposed)[0]:
        # Guard-blocked text is NEVER applied and never proposed (fail-closed).
        return "guard_blocked"

    from core.db import get_engine

    with Session(get_engine()) as session:
        # Defensive manual re-check: the active row may have been manually tuned
        # during the (network) classify.
        if _manual_block(session, scope_kind, scope_id, now):
            return "manual_block"

        current_text, prior_version = _current_detail(session, scope_kind, scope_id)
        gated = len(evidence_ids) >= min_evidence and _length_ok(current_text, proposed)
        baseline = ambient_analysis.score_window(
            session, scope_kind, scope_id, since, now
        )

        if gated:
            if mode == "live":
                if scope_kind == "channel":
                    target_version = directives.insert_active_directive(
                        session, scope_id, proposed, source="autopilot"
                    )
                else:
                    directives.insert_active_pref(
                        session, scope_id, proposed, source="autopilot"
                    )
                    target_version = 0
                status = "pending_validation"
                validate_days = _int_env(
                    "AUTOPILOT_VALIDATE_DAYS", _DEFAULT_VALIDATE_DAYS
                )
                validate_after = now + timedelta(days=validate_days)
            else:
                # Shadow: record what it WOULD apply, mutate nothing.
                target_version = 0
                status = "shadow"
                validate_after = now
            _write_log(
                session,
                scope_kind=scope_kind,
                scope_id=scope_id,
                target_version=target_version,
                prior_version=prior_version,
                prior_text=current_text,
                baseline=baseline,
                rationale=rationale,
                evidence_ids=evidence_ids,
                status=status,
                now=now,
                validate_after=validate_after,
            )
            session.commit()
            return status

        # Confident + guard-safe but ungated: record for out-of-band review.
        if mode != "live":
            status = "shadow"
        elif scope_kind == "channel":
            # Stage an inactive proposal row (same shape propose_update produces)
            # WITHIN this session so it commits atomically with the log and never
            # opens a nested session. No Discord message is posted, so the
            # autopilot stays silent; a human applies it via the MCP set-directive
            # tool. The synthetic proposal id is unguessable, so a stray reaction
            # can never confirm it.
            current = _active_channel_row(session, scope_id)
            session.add(
                ChannelDirective(
                    channel_id=scope_id,
                    directive=proposed,
                    version=(current.version if current is not None else 0) + 1,
                    active=False,
                    source="autopilot",
                    updated_by_user_id="autopilot",
                    proposal_message_id=f"autopilot:{uuid4().hex}",
                    previous_version=current.version if current is not None else 0,
                )
            )
            status = "proposed"
        else:
            status = "suggested"
        _write_log(
            session,
            scope_kind=scope_kind,
            scope_id=scope_id,
            target_version=0,
            prior_version=prior_version,
            prior_text=current_text,
            baseline=baseline,
            rationale=rationale,
            evidence_ids=evidence_ids,
            status=status,
            now=now,
            validate_after=now,
        )
        session.commit()
        return status


# --- VALIDATE phase ----------------------------------------------------------


def _fetch_pending(now: datetime) -> list[int]:
    """Ids of pending_validation rows whose validate_after has passed."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        rows = session.exec(
            select(DirectiveAutopilot)
            .where(DirectiveAutopilot.status == "pending_validation")
            .where(DirectiveAutopilot.validate_after <= now)
            .order_by(DirectiveAutopilot.id)
        ).all()
        return [r.id for r in rows if r.id is not None]


def _validate_one(row_id: int, now: datetime, mode: str) -> str:
    """Keep / revert / supersede one pending_validation row. Returns the outcome.

    Recomputes the post-apply score over [applied_at, now]. Manual precedence
    wins: if the scope's active row is now source='manual', the autopilot stands
    down (superseded_manual). A post score more than AUTOPILOT_REGRESS_MARGIN
    below the stored baseline reverts (reinstating the stored prior text); an
    equal-or-better score, or too little post-apply data to judge, keeps. In
    shadow mode a would-be revert is deferred (the row stays pending for a later
    live run) so shadow mutates nothing.
    """
    from core.db import get_engine

    with Session(get_engine()) as session:
        da = session.get(DirectiveAutopilot, row_id)
        if da is None or da.status != "pending_validation":
            return "skip"
        scope_kind, scope_id = da.scope_kind, da.scope_id

        if _active_source(session, scope_kind, scope_id) == "manual":
            da.status = "superseded_manual"
            session.add(da)
            session.commit()
            return "superseded_manual"

        baseline = None
        try:
            baseline = json.loads(da.baseline_json).get("score")
        except (ValueError, AttributeError):
            baseline = None
        # Bound the post-apply window with the stored applied_at as-is: it shares
        # the created_at columns' awareness (both tz-aware in Postgres, both naive
        # in SQLite tests), so a SQL range compare stays valid. Coercing only one
        # side would mix aware/naive and mis-bind under SQLite.
        post = ambient_analysis.score_window(
            session, scope_kind, scope_id, da.applied_at, now
        )
        margin = _float_env("AUTOPILOT_REGRESS_MARGIN", _DEFAULT_REGRESS_MARGIN)

        regressed = (
            baseline is not None and post is not None and post < baseline - margin
        )
        if regressed:
            if mode != "live":
                # Kill switch engaged: defer the revert to a later live run.
                return "deferred"
            # Reinstate the stored prior text WITHIN this session (session-param
            # primitives, no nested own session) so the revert and the status
            # flip commit atomically.
            if scope_kind == "channel":
                directives.insert_active_directive(
                    session, scope_id, da.prior_text or "", source="autopilot"
                )
            else:
                directives.insert_active_pref(
                    session, scope_id, da.prior_text or "", source="autopilot"
                )
            da.status = "reverted"
        else:
            da.status = "kept"
        session.add(da)
        session.commit()
        return da.status


# --- handler -----------------------------------------------------------------


async def directive_autopilot_handler(session: Session) -> None:
    """Run one autopilot pass: VALIDATE pending rows, then APPLY new refinements.

    The ``session`` argument (passed by the one-shot CLI wrapper) is unused: all
    DB I/O runs in worker threads via ``asyncio.to_thread`` with their own
    sessions. The Argo cron schedule drives cadence. Every scope is handled
    fail-closed: a classify or DB error on one scope is logged and skipped, never
    applied and never fatal to the run.
    """
    mode = _mode()
    now = datetime.now(timezone.utc)

    # Phase 1: VALIDATE (first, so a regression is reverted before new applies).
    pending = await asyncio.to_thread(_fetch_pending, now)
    for row_id in pending:
        try:
            outcome = await asyncio.to_thread(_validate_one, row_id, now, mode)
            logger.info("autopilot validate: row %d -> %s", row_id, outcome)
        except Exception:
            logger.exception("autopilot validate: row %d failed", row_id)

    # Phase 2: APPLY.
    lookback = _int_env("AUTOPILOT_LOOKBACK_DAYS", _DEFAULT_LOOKBACK_DAYS)
    since = now - timedelta(days=lookback)
    candidates = await asyncio.to_thread(_gather_candidates, since, now)
    if not candidates:
        logger.info("autopilot apply: no eligible scopes (mode=%s)", mode)
        return

    from chat.summarizer import build_llm_caller

    caller = build_llm_caller()
    acted = 0
    for candidate in candidates:
        try:
            result = await ambient_analysis.classify_scope(
                caller,
                candidate["scope_kind"],
                candidate["scope_id"],
                candidate["episodes"],
                candidate["current_directive"],
            )
            outcome = await asyncio.to_thread(
                _apply_or_log, candidate, result, since, now, mode
            )
            if outcome not in ("abstain", "guard_blocked", "manual_block"):
                acted += 1
            logger.info(
                "autopilot apply: %s %s -> %s",
                candidate["scope_kind"],
                candidate["scope_id"],
                outcome,
            )
        except Exception:
            logger.exception(
                "autopilot apply: %s %s failed",
                candidate["scope_kind"],
                candidate["scope_id"],
            )

    logger.info(
        "autopilot: mode=%s validated=%d candidates=%d acted=%d",
        mode,
        len(pending),
        len(candidates),
        acted,
    )
