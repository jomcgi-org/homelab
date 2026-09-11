"""Durable operator authority and conservative factory start reservations.

Every mutation locks the singleton control row before inspecting receipts. The
write lock also serializes file-backed SQLite tests; admission never relies on
an unlocked count. Issue text and conductor artifacts cannot configure policy.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Iterator

from sqlalchemy import update
from sqlmodel import Session, select

from core.db import get_engine
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask

_ACTIVE = ("admitted", "uncertain")
_TERMINAL = ("succeeded", "failed", "cancelled")
_POLICY_KEYS = {
    "repo",
    "issue_numbers",
    "generation",
    "max_tasks",
    "max_turns_per_task",
    "max_task_turns_hard",
    "max_parallel_nodes",
    "max_planner_turns",
    "task_budget_usd",
    "turn_budget_usd",
    "allowed_models",
    "conductor_model",
    "reviewer_model",
    "base_branch",
    "turn_timeout_seconds",
    "max_attempts",
    "worker_model",
    "task_timeout_seconds",
    "model_pools",
    "max_review_rounds",
    "intake",
}
_OPTIONAL_POLICY_KEYS = {
    "reviewer_model",
    "model_pools",
    "max_planner_turns",
    "max_review_rounds",
    "max_task_turns_hard",
    "max_turns_per_task",
    "max_parallel_nodes",
    "intake",
}
# Bounded review, correct and re-review rounds the engine runs on its own before
# it asks the planner. Absent from a live policy means this default, so the
# server gains the bound without an operator re-post.
DEFAULT_MAX_REVIEW_ROUNDS = 2
# Autonomous intake is off until an operator turns it on. A policy that
# predates the block reads these defaults, so the lane gains the shape
# without gaining the behaviour.
DEFAULT_INTAKE = {
    "enabled": False,
    "labels": ["agent-ready"],
    "exclude_labels": ["needs-human", "wontfix", "security-finding"],
    "max_per_day": 5,
    "cooldown_hours": 24,
    "refine_enabled": False,
}
DELIVER = "deliver"
REFINE = "refine"
# Nodes the reconciler may hold in flight for one task at once. One preserves
# the serial lane, so a policy written before fan-out existed never fans out.
DEFAULT_MAX_PARALLEL_NODES = 1
# Attempts the engine gives each node of a review round it inserts. A correction
# that fails is a deviation the planner must answer, not a turn to spend again,
# so a round costs exactly two turns and the reserve can say so honestly.
REVIEW_ROUND_ATTEMPTS = 1
_POOL_ROLES = {"conductor": "conductor_model", "worker": "worker_model"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def _money(value: object, name: str, *, zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    if value < 0 or (not zero and value == 0):
        raise ValueError(f"invalid {name}")
    return float(value)


def normalize_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise ValueError("invalid repo")
    if len(repo) > 256 or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("invalid repo")
    return repo.lower()


def validate_policy(policy: dict) -> dict:
    if (
        not isinstance(policy, dict)
        or not (_POLICY_KEYS - _OPTIONAL_POLICY_KEYS) <= set(policy) <= _POLICY_KEYS
    ):
        raise ValueError("policy must contain exactly the supported operator fields")
    result = dict(policy)
    if "reviewer_model" not in result:
        result["reviewer_model"] = policy["conductor_model"]
    result["repo"] = normalize_repo(policy["repo"])
    issues = policy["issue_numbers"]
    if not isinstance(issues, list) or not 1 <= len(issues) <= 100:
        raise ValueError("invalid issue_numbers")
    result["issue_numbers"] = sorted(
        {_integer(i, "issue_number", 1, 2**31 - 1) for i in issues}
    )
    for key, low, high in (
        ("generation", 0, 2**31 - 1),
        ("max_tasks", 1, 100),
        ("turn_timeout_seconds", 1, 43200),
        ("max_attempts", 1, 10),
        ("task_timeout_seconds", 1, 86400),
    ):
        result[key] = _integer(policy[key], key, low, high)
    # The accepted plan sizes the task; policy keeps only the envelope it must
    # fit inside. max_turns_per_task was the old fixed cap, so it is accepted
    # as that envelope and a live policy needs no re-post.
    if "max_turns_per_task" in policy:
        result["max_turns_per_task"] = _integer(
            policy["max_turns_per_task"], "max_turns_per_task", 1, 100
        )
    if "max_task_turns_hard" in policy:
        result["max_task_turns_hard"] = _integer(
            policy["max_task_turns_hard"], "max_task_turns_hard", 1, 500
        )
    elif "max_turns_per_task" in policy:
        result["max_task_turns_hard"] = result["max_turns_per_task"]
    else:
        raise ValueError("policy needs max_task_turns_hard or max_turns_per_task")
    # Absent means the serial lane this reconciler shipped with, so raising it
    # is an explicit operator act rather than a side effect of this change.
    result["max_parallel_nodes"] = _integer(
        policy.get("max_parallel_nodes", DEFAULT_MAX_PARALLEL_NODES),
        "max_parallel_nodes",
        1,
        8,
    )
    # Planning rounds are capped separately from delivery work. An absent field
    # means a policy written before the split, so it inherits the envelope
    # rather than forcing an operator to re-post a live policy.
    result["max_planner_turns"] = (
        _integer(policy["max_planner_turns"], "max_planner_turns", 1, 100)
        if "max_planner_turns" in policy
        else result["max_task_turns_hard"]
    )
    result["max_review_rounds"] = _integer(
        policy.get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS),
        "max_review_rounds",
        0,
        10,
    )
    for key in ("task_budget_usd", "turn_budget_usd"):
        result[key] = _money(policy[key], key)
    if result["turn_budget_usd"] > result["task_budget_usd"]:
        raise ValueError("turn budget exceeds task budget")
    models = policy["allowed_models"]
    if not isinstance(models, list) or not 1 <= len(models) <= 16:
        raise ValueError("invalid allowed_models")
    result["allowed_models"] = sorted({_text(m, "model", 128) for m in models})
    if policy["conductor_model"] not in result["allowed_models"]:
        raise ValueError("conductor model is not allowed")
    if result["reviewer_model"] not in result["allowed_models"]:
        raise ValueError("reviewer model is not allowed")
    if policy["worker_model"] not in result["allowed_models"]:
        raise ValueError("worker model is not allowed")
    if "model_pools" in policy:
        result["model_pools"] = _validate_model_pools(policy["model_pools"], result)
    result["intake"] = _validate_intake(policy.get("intake", {}))
    if result["turn_timeout_seconds"] > result["task_timeout_seconds"]:
        raise ValueError("turn timeout exceeds task timeout")
    result["base_branch"] = _text(policy["base_branch"], "base_branch", 256)
    return result


def _validate_intake(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= set(DEFAULT_INTAKE):
        raise ValueError("invalid intake")
    result = {}
    for key in ("enabled", "refine_enabled"):
        setting = value.get(key, DEFAULT_INTAKE[key])
        if type(setting) is not bool:
            raise ValueError(f"invalid {key}")
        result[key] = setting
    for key in ("labels", "exclude_labels"):
        labels = value.get(key, DEFAULT_INTAKE[key])
        if not isinstance(labels, list) or len(labels) > 32:
            raise ValueError(f"invalid {key}")
        result[key] = sorted({_text(label, "label", 128) for label in labels})
    result["max_per_day"] = _integer(
        value.get("max_per_day", DEFAULT_INTAKE["max_per_day"]),
        "max_per_day",
        1,
        50,
    )
    result["cooldown_hours"] = _integer(
        value.get("cooldown_hours", DEFAULT_INTAKE["cooldown_hours"]),
        "cooldown_hours",
        1,
        168,
    )
    if set(result["labels"]) & set(result["exclude_labels"]):
        raise ValueError("intake labels overlap exclude_labels")
    return result


def intake_policy(policy: dict) -> dict:
    """The intake block, defaulted, so a policy stored before it reads as off."""
    return _validate_intake(policy.get("intake") or {})


def receipt_kind(row) -> str:
    """A receipt written before refine tasks existed is a delivery."""
    return row.kind or DELIVER


def _validate_model_pools(pools: object, policy: dict) -> dict:
    """Each pool is an ordered preference list headed by the role's own model."""
    if not isinstance(pools, dict) or not pools or not set(pools) <= set(_POOL_ROLES):
        raise ValueError("invalid model_pools")
    result = {}
    for role, pool in pools.items():
        if not isinstance(pool, list) or not 1 <= len(pool) <= 8:
            raise ValueError(f"invalid model_pools.{role}")
        models = [_text(m, "model", 128) for m in pool]
        if len(set(models)) != len(models):
            raise ValueError(f"duplicate model in model_pools.{role}")
        if models[0] != policy[_POOL_ROLES[role]]:
            raise ValueError(f"model_pools.{role} must start with the {role} model")
        if any(m not in policy["allowed_models"] for m in models):
            raise ValueError(f"model_pools.{role} names a model that is not allowed")
        result[role] = models
    return result


@contextmanager
def _read_session(session: Session | None = None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with Session(get_engine()) as owned:
            yield owned


@contextmanager
def _locked_session(
    session: Session | None = None,
) -> Iterator[tuple[Session, FactoryControl]]:
    """Supplied sessions retain their transaction for atomic caller composition."""
    with _read_session(session) as db:
        try:
            result = db.execute(
                update(FactoryControl)
                .where(FactoryControl.id == "factory")
                .values(version=FactoryControl.version)
            )
            if result.rowcount != 1:
                raise RuntimeError("factory control migration/seed is missing")
            control = db.exec(
                select(FactoryControl)
                .where(FactoryControl.id == "factory")
                .execution_options(populate_existing=True)
            ).one()
            yield db, control
            if session is None:
                db.commit()
            else:
                db.flush()
        except BaseException:
            if session is None:
                db.rollback()
            raise


def _audit(
    db: Session,
    actor: str,
    action: str,
    *,
    task_id: str | None = None,
    **detail: object,
) -> None:
    if task_id is not None and db.get(SwarmTask, task_id) is None:
        detail["requested_task_id"] = task_id
        task_id = None
    db.add(
        FactoryAudit(
            actor=actor, action=action, task_id=task_id, detail_json=_json(detail)
        )
    )


def _receipt(db: Session, task_id: str) -> FactoryReceipt | None:
    return db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.task_id == task_id)
        .execution_options(populate_existing=True)
    ).first()


def _starts(db: Session, task_id: str) -> list[FactoryStart]:
    return list(
        db.exec(
            select(FactoryStart)
            .where(FactoryStart.task_id == task_id)
            .order_by(FactoryStart.id)
            .execution_options(populate_existing=True)
        ).all()
    )


# Start keys are "factory-node:<task id>:<node key>:<attempt>". A node key may
# itself contain colons, so the attempt and the two fixed leading segments are
# what anchor the parse.
_START_KEY = re.compile(r"^factory-node:[^:]+:(?P<node_key>.+):[0-9]+$")


def _planner_key(start_key: str | None) -> bool:
    """True for a conductor planning round rather than a unit of task work.

    An unparsable key counts as work, so the turn cap can never be widened by
    a start whose shape this cannot read.
    """
    match = _START_KEY.match(start_key or "")
    return bool(match) and match.group("node_key").startswith("conductor_")


def _planner_start(row: FactoryStart) -> bool:
    return _planner_key(row.start_key)


def task_turn_ceiling(policy: dict) -> int:
    """The work-turn envelope no accepted plan may size past.

    A receipt pins its policy at admission, so a task admitted before
    max_task_turns_hard existed reads its old fixed cap as the envelope.
    """
    ceiling = policy.get("max_task_turns_hard")
    return policy["max_turns_per_task"] if ceiling is None else ceiling


def parallel_limit(policy: dict) -> int:
    """Nodes one task may hold in flight at once, serial unless raised."""
    limit = policy.get("max_parallel_nodes")
    return DEFAULT_MAX_PARALLEL_NODES if limit is None else limit


def planner_turn_cap(policy: dict) -> int:
    """The planning round cap, inherited from the envelope by older policies.

    A receipt pins its policy at admission, so a task admitted before
    max_planner_turns existed still needs a bound.
    """
    cap = policy.get("max_planner_turns")
    return task_turn_ceiling(policy) if cap is None else cap


def allowance_from_graph(
    nodes: list[dict],
    runs: list[dict],
    policy: dict,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    graph_revision: int,
    reviewable: bool | None = None,
) -> dict:
    """Size a task from the plan it accepted, not from a fixed policy number.

    Work turns are one per attempt the live graph can still spend, plus every
    work turn history already spent, plus the nodes the engine may still insert
    on its own. Review rounds are reserved lazily: only the next one, and at the
    two turns it really costs, a correction and a re-review each inserted at
    ``REVIEW_ROUND_ATTEMPTS``. Reserving every remaining round up front priced a
    loop the task would probably never open, and it made a modest envelope
    unable to hold its own plan. The allowance instead grows by one round at a
    time, as each round is inserted and the next one is reserved behind it, and
    every insertion is checked against the envelope as it happens. A fan-in is
    one node at the policy's ``max_attempts``, exactly as the inserted node will
    carry, so a fan-in stays turn-neutral. Review rounds are reserved only when
    the plan holds a review node that could open one.

    An attempt already spent is counted in history, so its node slot is not
    counted again; before anything runs the two readings agree. Dollars are the
    same shape in money: charged history plus every live unsucceeded node's
    unspent ceiling, plus the same prospective insertions, each at one per-turn
    ceiling because a node's ceiling is shared across its attempts.

    A discarded node stops contributing its remaining slots, and its spent
    attempts stay in history, so discarding never refunds a consumed turn.

    ``reviewable`` overrides whether these nodes could open a round at all. A
    refusal reads the live graph under the reserve the refused edit implies, so
    an edit that adds the first review node is sized against the round that
    review would open rather than against a graph that has none.
    """
    attempts: dict[str, int] = {}
    charged: dict[str, float] = {}
    succeeded: set[str] = set()
    work_turns_used = 0
    charged_total = 0.0
    for run in runs:
        key = run["node_key"]
        cost = float(run["accounted_cost_usd"])
        attempts[key] = attempts.get(key, 0) + 1
        charged[key] = charged.get(key, 0.0) + cost
        charged_total += cost
        if run["status"] == "succeeded":
            succeeded.add(key)
        if not key.startswith("conductor_"):
            work_turns_used += 1
    remaining_turns = 0
    remaining_usd = 0.0
    for node in nodes:
        key = node["node_key"]
        if key in succeeded:
            continue
        remaining_usd += max(0.0, node["max_cost_usd"] - charged.get(key, 0.0))
        if key.startswith("conductor_"):
            continue
        remaining_turns += max(0, node["max_attempts"] - attempts.get(key, 0))
    if reviewable is None:
        reviewable = any(node["node_key"].startswith("review_") for node in nodes)
    rounds = min(1, max(0, review_rounds_remaining)) if reviewable else 0
    fan_ins = max(0, fan_ins_remaining)
    reserved_nodes = 2 * rounds + fan_ins
    reserved_turns = (
        2 * rounds * REVIEW_ROUND_ATTEMPTS + fan_ins * policy["max_attempts"]
    )
    return {
        "turns": work_turns_used + remaining_turns + reserved_turns,
        "usd": round(
            charged_total + remaining_usd + reserved_nodes * policy["turn_budget_usd"],
            6,
        ),
        "graph_revision": graph_revision,
        "review_rounds_reserved": rounds,
        "fan_ins_reserved": fan_ins,
        "derived": True,
    }


def derive_allowance(
    task_id: str,
    policy: dict,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    session: Session | None = None,
) -> dict:
    """Read the live graph and size the task from it."""
    from swarm import graph

    with _read_session(session) as db:
        return allowance_from_graph(
            graph.load_graph(task_id, session=db),
            graph.node_runs(task_id, session=db),
            policy,
            review_rounds_remaining=review_rounds_remaining,
            fan_ins_remaining=fan_ins_remaining,
            graph_revision=graph.current_version(task_id, session=db),
        )


def envelope_excess(
    allowance: dict, policy: dict, *, accounted: dict | None = None
) -> dict | None:
    """Name what a derived allowance overspends, or None when it fits.

    ``accounted`` is what the task is already sized at, before the refused
    edit, under the reserve that edit implies. Given it, the refusal also
    carries ``spare_turns`` and ``spare_usd``, what the envelope would still
    fund, so the planner can size a smaller edit against whichever of the two
    is binding instead of re-proposing the one that was just refused.
    """
    turns_allowed = task_turn_ceiling(policy)
    usd_allowed = policy["task_budget_usd"]
    if allowance["turns"] <= turns_allowed and allowance["usd"] <= usd_allowed:
        return None
    excess = {
        "turns": {"needed": allowance["turns"], "allowed": turns_allowed},
        "usd": {"needed": allowance["usd"], "allowed": usd_allowed},
    }
    if accounted is not None:
        excess["spare_turns"] = max(0, turns_allowed - accounted["turns"])
        excess["spare_usd"] = round(max(0.0, usd_allowed - accounted["usd"]), 6)
    return excess


def _stored_allowance(row: FactoryReceipt, policy: dict) -> dict:
    """The plan-derived allowance, or the envelope until a plan derives one.

    A task admitted before this column existed, or one whose planner has not
    produced an accepted plan yet, has nothing derived. The envelope is the
    honest fallback: it is what the old fixed cap already meant.
    """
    if row.allowance_json:
        stored = json.loads(row.allowance_json)
        if isinstance(stored, dict) and type(stored.get("turns")) is int:
            return stored
    return {
        "turns": task_turn_ceiling(policy),
        "usd": policy["task_budget_usd"],
        "graph_revision": None,
        "review_rounds_reserved": 0,
        "fan_ins_reserved": 0,
        "derived": False,
    }


def task_allowance(task_id: str, *, session: Session | None = None) -> dict:
    """The allowance currently stored for a task, or the envelope standing in."""
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        if row is None or not row.policy_json:
            return {"turns": 0, "usd": 0.0, "graph_revision": None, "derived": False}
        return _stored_allowance(row, json.loads(row.policy_json))


def record_allowance(
    task_id: str,
    policy: dict,
    actor: str,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    cause: str | None = None,
    session: Session | None = None,
) -> dict:
    """Recompute and persist the allowance the current graph revision implies."""
    actor = _text(actor, "actor")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        allowance = derive_allowance(
            task_id,
            policy,
            review_rounds_remaining=review_rounds_remaining,
            fan_ins_remaining=fan_ins_remaining,
            session=db,
        )
        # Acceptance refuses an over-envelope plan, so this can only clamp a
        # graph that reached the server another way. Clamping never widens the
        # envelope after the fact.
        allowance["turns"] = min(allowance["turns"], task_turn_ceiling(policy))
        allowance["usd"] = min(allowance["usd"], policy["task_budget_usd"])
        row.allowance_json = _json(allowance)
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            actor,
            "allowance_derived",
            task_id=task_id,
            cause=cause,
            allowance=allowance,
        )
        return {"ok": True, "allowance": allowance}


def _accounting(starts: list[FactoryStart]) -> dict:
    # Planner rounds read evidence and decide; they do not do the task's work.
    # Counting them against max_turns_per_task exhausts a task before it has
    # spent its allowance on delivery. They still consume budget.
    planner = sum(_planner_start(row) for row in starts)
    return {
        "turns_used": len(starts) - planner,
        "planner_turns_used": planner,
        "committed_cost_usd": sum(
            max(row.max_cost_usd, row.cost_usd or 0)
            if row.status in ("reserved", "uncertain")
            else row.max_cost_usd
            if row.cost_usd is None
            else row.cost_usd
            for row in starts
        ),
        "unresolved_starts": sum(
            row.status in ("reserved", "uncertain") for row in starts
        ),
    }


def _start_dict(row: FactoryStart) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "task_id",
            "start_key",
            "actor",
            "model",
            "max_cost_usd",
            "status",
            "cost_usd",
            "session_id",
        )
    }


def _snapshot(db: Session, row: FactoryReceipt, *, body: bool = False) -> dict:
    starts = _starts(db, row.task_id) if row.task_id else []
    result = {
        key: getattr(row, key)
        for key in (
            "id",
            "repo",
            "issue_number",
            "generation",
            "title",
            "url",
            "state",
            "task_id",
            "task_paused",
            "cancellation_requested",
        )
    }
    result["kind"] = receipt_kind(row)
    result.update(
        policy=json.loads(row.policy_json) if row.policy_json else None,
        starts=[_start_dict(s) for s in starts],
        **_accounting(starts),
    )
    if row.task_id:
        # Bounded lifecycle evidence on the existing task/status surface. Never
        # return raw stop identity hashes, turn bodies or execution credentials.
        stop_rows = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action.in_(
                    (
                        "stop_intent",
                        "stop_request",
                        "stop_accepted",
                        "stop_observation",
                        "stop_settled",
                        "attempt_stop_requested",
                        "attempt_stop_cancel",
                    )
                ),
            )
            .order_by(FactoryAudit.id.desc())
            .limit(16)
        ).all()
        result["stop_events"] = []
        for event in stop_rows:
            detail = json.loads(event.detail_json)
            identity = detail.get("identity") or {}
            result["stop_events"].append(
                {
                    "action": event.action,
                    "actor": event.actor,
                    "request_key": detail.get("request_key"),
                    "created_at": event.created_at.isoformat(),
                    "workflow_id": detail.get("workflow_id"),
                    "session_id": detail.get("session_id", identity.get("session_id")),
                    "guest_id": detail.get("guest_id", identity.get("guest_id")),
                    **{
                        key: detail[key]
                        for key in (
                            "reason",
                            "request_number",
                            "cessation_confirmed",
                            "intervention_required",
                        )
                        if key in detail
                    },
                }
            )
        task = db.get(SwarmTask, row.task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        deadline = admitted + timedelta(
            seconds=result["policy"]["task_timeout_seconds"]
        )
        allowance = _stored_allowance(row, result["policy"])
        result["allowance"] = allowance
        result.update(
            admitted_at=admitted.isoformat(),
            deadline_at=deadline.isoformat(),
            limits={
                "deadline_expired": _now() >= deadline,
                "turn_limit_reached": result["turns_used"] >= allowance["turns"],
                "planner_turn_limit_reached": result["planner_turns_used"]
                >= planner_turn_cap(result["policy"]),
                "budget_limit_reached": result["committed_cost_usd"]
                >= result["policy"]["task_budget_usd"],
            },
        )
        last = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "finish_task",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        result["evidence"] = (
            json.loads(last.detail_json).get("evidence") if last else None
        )
    if body:
        result["body"] = row.body
    return result


def intake_state(policy: dict, *, session: Session | None = None) -> dict:
    """What the board shows: the block, the last two audits, today's usage.

    This lives beside status rather than beside the intake loop because the
    board reads it, and the board must not link the reconciler to render a
    policy.
    """
    block = intake_policy(policy)
    cutoff = _now() - timedelta(hours=24)
    with _read_session(session) as db:
        admitted_today = len(
            db.exec(
                select(FactoryAudit.id).where(
                    FactoryAudit.action == "intake_admitted",
                    FactoryAudit.created_at >= cutoff,
                )
            ).all()
        )

        def latest(action: str) -> dict | None:
            row = db.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id.desc())
            ).first()
            if row is None:
                return None
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return {
                "created_at": created.isoformat(),
                "detail": json.loads(row.detail_json),
            }

        return {
            "policy": block,
            "admitted_today": admitted_today,
            "max_per_day": block["max_per_day"],
            "last_admitted": latest("intake_admitted"),
            "last_idle": latest("intake_idle"),
        }


def status(*, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        if control is None:
            return {
                "ok": False,
                "reason": "not_initialized",
                "state": "disabled",
                "receipts": [],
            }
        rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        receipts = [_snapshot(db, r) for r in rows]
        policy = json.loads(control.policy_json)
        return {
            "ok": True,
            "state": control.state,
            "policy": policy,
            "intake": intake_state(policy, session=db),
            "admitted_count": control.admitted_count,
            "version": control.version,
            "actor": control.actor,
            "receipts": receipts,
            "active_tasks": [r for r in receipts if r["state"] in _ACTIVE],
        }


def task_snapshot(task_id: str, *, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        return (
            {"ok": False, "reason": "unknown_task"}
            if row is None
            else {"ok": True, **_snapshot(db, row, body=True)}
        )


def set_control(
    action: str,
    actor: str,
    *,
    policy: dict | None = None,
    task_id: str | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if action not in (
        "configure",
        "enable",
        "pause_admissions",
        "pause_task",
        "resume_task",
        "stop",
    ):
        raise ValueError("invalid control action")
    configured = validate_policy(policy) if action == "configure" else None
    if action != "configure" and policy is not None:
        raise ValueError("policy requires configure action")
    if (action in ("pause_task", "resume_task")) != (task_id is not None):
        raise ValueError("task_id is required only for task pause/resume")
    with _locked_session(session) as (db, control):
        reason = None
        if control.state == "stopped" and action != "stop":
            reason = "stopped"
        elif action == "configure":
            if db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).first():
                reason = "active_task"
            else:
                control.policy_json = _json(configured)
                control.state = "disabled"
        elif action == "enable":
            if not json.loads(control.policy_json):
                reason = "not_configured"
            else:
                control.state = "enabled"
        elif action == "pause_admissions":
            control.state = "paused"
        elif action in ("pause_task", "resume_task"):
            row = _receipt(db, task_id)
            if row is None or row.state not in _ACTIVE:
                reason = "task_not_active"
            elif row.cancellation_requested:
                reason = "cancellation_pending"
            else:
                row.task_paused = action == "pause_task"
                row.updated_at = _now()
                db.add(row)
        elif action == "stop":
            control.state = "stopped"
            control.stopped_at = control.stopped_at or _now()
            for row in db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).all():
                row.cancellation_requested = True
                row.updated_at = _now()
                db.add(row)
        if reason is None:
            control.version += 1
            control.updated_at = _now()
            control.actor = actor
            db.add(control)
        _audit(
            db,
            actor,
            action,
            task_id=task_id,
            ok=reason is None,
            reason=reason,
            policy=configured,
        )
        return {
            "ok": reason is None,
            "reason": reason,
            "state": control.state,
            "version": control.version,
        }


def _can_start(db: Session, control: FactoryControl, task_id: str) -> dict:
    row = _receipt(db, task_id)
    reason = None
    if control.state == "stopped":
        reason = "stopped"
    elif control.state not in ("enabled", "paused"):
        reason = "disabled"
    elif row is None or row.state not in _ACTIVE:
        reason = "task_not_active"
    elif row.cancellation_requested:
        reason = "cancellation_pending"
    elif row.task_paused:
        reason = "task_paused"
    elif row.state == "uncertain" or any(
        s.status == "uncertain" for s in _starts(db, task_id)
    ):
        reason = "uncertain_outcome"
    elif row is not None:
        task = db.get(SwarmTask, task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        timeout = json.loads(row.policy_json)["task_timeout_seconds"]
        if _now() >= admitted + timedelta(seconds=timeout):
            reason = "task_deadline"
    return {"ok": reason is None, "reason": reason}


def can_start(task_id: str, *, session: Session | None = None) -> dict:
    """Fresh read fence. Use start_guard to serialize it with session creation."""
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        return (
            {"ok": False, "reason": "not_initialized"}
            if control is None
            else _can_start(db, control, task_id)
        )


@contextmanager
def start_guard(task_id: str, *, session: Session | None = None) -> Iterator[dict]:
    """Serialize stop with synchronous session persistence, never guest waits.

    Hold this guard only across the API that durably creates the session and
    pending turn. Once it returns, an admitted session remains subject to explicit
    cancellation/reconciliation. A supplied session must commit before any wait.
    """
    with _locked_session(session) as (db, control):
        yield _can_start(db, control, task_id)


def authorize_start(
    task_id: str,
    start_key: str,
    actor: str,
    *,
    model: str,
    max_cost_usd: float,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    start_key = _text(start_key, "start_key", 256)
    model = _text(model, "model", 128)
    cost = _money(max_cost_usd, "max_cost_usd")
    with _locked_session(session) as (db, control):
        check = _can_start(db, control, task_id)
        if not check["ok"]:
            return check
        row = _receipt(db, task_id)
        policy = json.loads(row.policy_json)
        starts = _starts(db, task_id)
        existing = next((s for s in starts if s.start_key == start_key), None)
        if existing:
            if existing.model != model or existing.max_cost_usd != cost:
                return {"ok": False, "reason": "conflicting_start_pin"}
            return {"ok": True, "replayed": True, "start": _start_dict(existing)}
        budget = _accounting(starts)
        planner = _planner_key(start_key)
        reason = None
        # One reserved start per parallel slot. At the default limit of one
        # this is the original single-flight fence, unchanged.
        if sum(s.status == "reserved" for s in starts) >= parallel_limit(policy):
            reason = "start_pending"
        elif model not in policy["allowed_models"]:
            reason = "model_not_allowed"
        # A planning round is cheap enough that the task budget alone would
        # admit hundreds of them, and a refused decision can mint the next
        # planner every tick, so deliberation is bounded on its own count.
        elif planner and budget["planner_turns_used"] >= planner_turn_cap(policy):
            reason = "planner_turn_limit"
        # The plan sizes the work, so the bound is the derived allowance rather
        # than a policy number. Nothing but an accepted plan edit moves it.
        elif (
            not planner
            and budget["turns_used"] >= _stored_allowance(row, policy)["turns"]
        ):
            reason = "turn_limit"
        # Both kinds of start still answer to the one task budget.
        elif (
            cost > policy["turn_budget_usd"]
            or budget["committed_cost_usd"] + cost > policy["task_budget_usd"]
        ):
            reason = "budget_limit"
        if reason:
            _audit(
                db,
                actor,
                "authorize_start",
                task_id=task_id,
                ok=False,
                reason=reason,
                start_key=start_key,
            )
            return {"ok": False, "reason": reason}
        start = FactoryStart(
            task_id=task_id,
            start_key=start_key,
            actor=actor,
            model=model,
            max_cost_usd=cost,
        )
        db.add(start)
        db.flush()
        _audit(
            db, actor, "authorize_start", task_id=task_id, ok=True, start_key=start_key
        )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def record_start_outcome(
    task_id: str,
    start_key: str,
    status: str,
    actor: str,
    *,
    cost_usd: float | None = None,
    session_id: int | None = None,
    reconciled: bool = False,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if type(reconciled) is not bool:
        raise ValueError("invalid reconciled flag")
    if status not in (*_TERMINAL, "uncertain", "escalated"):
        raise ValueError("invalid start outcome")
    cost = None if cost_usd is None else _money(cost_usd, "cost_usd", zero=True)
    if session_id is not None:
        _integer(session_id, "session_id", 1, 2**31 - 1)
    # Known completion without provider cost consumes the reserved ceiling.
    # Only uncertain execution retains an active reservation.
    # Escalation is terminal work without delivery. Keep its richer graph
    # outcome while settling this reservation under the existing failed status.
    effective = "failed" if status == "escalated" else status
    with _locked_session(session) as (db, _control):
        start = next(
            (s for s in _starts(db, task_id) if s.start_key == start_key), None
        )
        if start is None:
            return {"ok": False, "reason": "unknown_start"}
        if (
            start.status == effective
            and start.cost_usd == cost
            and start.session_id == session_id
        ):
            return {"ok": True, "replayed": True, "start": _start_dict(start)}
        if start.status in _TERMINAL:
            return {"ok": False, "reason": "conflicting_outcome"}
        if start.status == "uncertain" and not reconciled:
            return {"ok": False, "reason": "reconciliation_required"}
        if start.session_id is not None and session_id != start.session_id:
            return {"ok": False, "reason": "conflicting_session"}
        start.status, start.cost_usd, start.session_id = effective, cost, session_id
        start.updated_at = _now()
        db.add(start)
        row = _receipt(db, task_id)
        if effective == "uncertain":
            row.state = "uncertain"
        elif row.state == "uncertain" and not any(
            s.status == "uncertain" and s.id != start.id for s in _starts(db, task_id)
        ):
            row.state = "admitted"
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            actor,
            "record_start_outcome",
            task_id=task_id,
            start_key=start_key,
            status=effective,
            reconciled=reconciled,
        )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def finish_task(
    task_id: str,
    outcome: str,
    actor: str,
    *,
    evidence: dict | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if outcome not in (*_TERMINAL, "uncertain"):
        raise ValueError("invalid task outcome")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - {
            "pr_url",
            "head_sha",
            "review_session_id",
            "state",
            "reason",
        }:
            raise ValueError("unsupported task evidence")
        for key, value in evidence.items():
            if key == "review_session_id":
                _integer(value, key, 1, 2**31 - 1)
            else:
                _text(value, key, 1024)
        if len(_json(evidence)) > 4096:
            raise ValueError("task evidence too large")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        if row.state in _TERMINAL:
            last = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "finish_task",
                )
                .order_by(FactoryAudit.id.desc())
            ).first()
            same = (
                row.state == outcome
                and last is not None
                and json.loads(last.detail_json).get("evidence") == evidence
            )
            return {"ok": same, "reason": None if same else "conflicting_outcome"}
        if (
            outcome != "uncertain"
            and _accounting(_starts(db, task_id))["unresolved_starts"]
        ):
            return {"ok": False, "reason": "unresolved_starts"}
        row.state = outcome
        row.updated_at = _now()
        db.add(row)
        if outcome in _TERMINAL:
            task = db.get(SwarmTask, task_id)
            task.settled_at = _now()
            task.start_state = outcome
            db.add(task)
        _audit(
            db,
            actor,
            "finish_task",
            task_id=task_id,
            outcome=outcome,
            evidence=evidence,
        )
        return {"ok": True, "state": outcome}
