"""Semantic operations for versioned swarm plan graphs.

ADR agents/062 defines the mutable DAG rules implemented here. Every mutation
locks the owning ``swarm_task`` row with ``SELECT ... FOR UPDATE`` and performs
all graph, ledger, and conductor-call writes in one transaction. That task row
lock is the single-flight-per-run primitive from ADR agents/062 open question 1.
SQLite treats the lock as a plain read, which is sufficient for hermetic tests.

Every applied operation and policy refusal is recorded according to #4781
decision 10. Policy refusals are returned as :class:`GraphOp` values. Only
programmer errors, such as an unknown task or invalid transition, raise.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Iterator

from sqlalchemy import func
from sqlmodel import Session, select

from core.db import get_engine
from swarm.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

_NODE_KINDS = ("work", "gate", "merge", "fable_escalation")
_TERMINAL_RUN_STATUSES = ("succeeded", "failed", "escalated", "cancelled")


@dataclass
class GraphOp:
    ok: bool
    version: int | None = None
    refusal_code: str | None = None
    detail: str | None = None


@contextmanager
def _session(session: Session | None = None) -> Iterator[Session]:
    if session is not None:
        yield session
        return
    with Session(get_engine()) as owned_session:
        yield owned_session


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _current_version(db: Session, task_id: str) -> int:
    version = db.exec(
        select(func.max(SwarmPlanVersion.version)).where(
            SwarmPlanVersion.task_id == task_id
        )
    ).one()
    return int(version or 0)


def current_version(task_id: str, *, session: Session | None = None) -> int:
    """Return the latest plan version for a task, or zero before bootstrap."""

    with _session(session) as db:
        return _current_version(db, task_id)


def _visible_nodes(db: Session, task_id: str, version: int) -> list[SwarmPlanNode]:
    return list(
        db.exec(
            select(SwarmPlanNode)
            .where(
                SwarmPlanNode.task_id == task_id,
                SwarmPlanNode.created_in_version <= version,
                (SwarmPlanNode.discarded_in_version.is_(None))
                | (SwarmPlanNode.discarded_in_version > version),
                (SwarmPlanNode.cancelled_in_version.is_(None))
                | (SwarmPlanNode.cancelled_in_version > version),
            )
            .order_by(SwarmPlanNode.id)
        ).all()
    )


def _node_deps(node: SwarmPlanNode) -> list[str]:
    deps = json.loads(node.deps_json)
    if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
        raise ValueError(f"Invalid deps_json for swarm node {node.node_key}")
    return deps


def load_graph(
    task_id: str,
    version: int | None = None,
    *,
    session: Session | None = None,
) -> list[dict]:
    """Load the nodes visible in one historical plan revision."""

    with _session(session) as db:
        selected_version = _current_version(db, task_id) if version is None else version
        return [
            {
                "node_key": node.node_key,
                "kind": node.kind,
                "prompt": node.prompt,
                "model": node.model,
                "deps": _node_deps(node),
                "max_cost_usd": node.max_cost_usd,
                "side_effects": node.side_effects,
                "max_attempts": node.max_attempts,
                "turn_timeout_seconds": node.turn_timeout_seconds,
                "created_in_version": node.created_in_version,
                "discarded_in_version": node.discarded_in_version,
                "cancelled_in_version": node.cancelled_in_version,
                "armed_at": node.armed_at,
                "base_artifact_sha": node.base_artifact_sha,
            }
            for node in _visible_nodes(db, task_id, selected_version)
        ]


def _lock_task(db: Session, task_id: str) -> SwarmTask:
    task = db.exec(
        select(SwarmTask).where(SwarmTask.id == task_id).with_for_update()
    ).first()
    if task is None:
        raise ValueError(f"Unknown swarm task {task_id}")
    return task


def _finish(
    db: Session,
    task: SwarmTask,
    tool: str,
    args: dict[str, Any],
    version_before: int,
    result: GraphOp,
) -> GraphOp:
    db.add(
        SwarmConductorCall(
            task_id=task.id,
            conductor_model=task.conductor_model,
            tool=tool,
            args_json=_json(args),
            outcome="applied" if result.ok else "refused",
            refusal_code=result.refusal_code,
            version_before=version_before,
            version_after=result.version,
        )
    )
    db.commit()
    return result


def _refuse(
    db: Session,
    task: SwarmTask,
    tool: str,
    args: dict[str, Any],
    version: int,
    code: str,
    detail: str | None = None,
) -> GraphOp:
    return _finish(
        db,
        task,
        tool,
        args,
        version,
        GraphOp(ok=False, version=version, refusal_code=code, detail=detail),
    )


def _would_cycle(
    node_key: str, deps: list[str], live_nodes: dict[str, SwarmPlanNode]
) -> bool:
    pending = list(deps)
    visited: set[str] = set()
    while pending:
        dependency = pending.pop()
        if dependency == node_key:
            return True
        if dependency in visited:
            continue
        visited.add(dependency)
        dependency_node = live_nodes.get(dependency)
        if dependency_node is not None:
            pending.extend(_node_deps(dependency_node))
    return False


def _fable_ever_created(db: Session, task_id: str) -> bool:
    if (
        db.exec(
            select(SwarmPlanNode.id).where(
                SwarmPlanNode.task_id == task_id,
                SwarmPlanNode.kind == "fable_escalation",
            )
        ).first()
        is not None
    ):
        return True
    versions = db.exec(
        select(SwarmPlanVersion.change_json).where(
            SwarmPlanVersion.task_id == task_id,
            SwarmPlanVersion.op == "add_node",
        )
    ).all()
    return any(
        json.loads(change).get("kind") == "fable_escalation" for change in versions
    )


def add_node(
    task_id: str,
    *,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
    stated_reason: str | None,
    expected_version: int,
    node_key: str,
    kind: str,
    prompt: str,
    model: str | None,
    deps: list[str],
    max_cost_usd: float,
    side_effects: bool,
    max_attempts: int | None,
    turn_timeout_seconds: int | None,
) -> GraphOp:
    """Add or re-add one node after validating the complete semantic change."""

    node_fields = {
        "node_key": node_key,
        "kind": kind,
        "prompt": prompt,
        "model": model,
        "deps": deps,
        "max_cost_usd": max_cost_usd,
        "side_effects": side_effects,
        "max_attempts": max_attempts,
        "turn_timeout_seconds": turn_timeout_seconds,
    }
    args = {
        "author_kind": author_kind,
        "author": author,
        "cause_kind": cause_kind,
        "cause_ref": cause_ref,
        "stated_reason": stated_reason,
        "expected_version": expected_version,
        **node_fields,
    }
    with _session() as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        if expected_version != version:
            return _refuse(db, task, "add_node", args, version, "stale_version")

        live = {node.node_key: node for node in _visible_nodes(db, task_id, version)}
        if node_key in live:
            return _refuse(db, task, "add_node", args, version, "duplicate_key")
        unknown = [dependency for dependency in deps if dependency not in live]
        if unknown:
            return _refuse(
                db,
                task,
                "add_node",
                args,
                version,
                "unknown_dep",
                detail=unknown[0],
            )
        if _would_cycle(node_key, deps, live):
            return _refuse(db, task, "add_node", args, version, "cycle")
        if kind not in _NODE_KINDS:
            return _refuse(db, task, "add_node", args, version, "invalid_kind")
        if kind == "fable_escalation" and task.budget_usd is None:
            return _refuse(db, task, "add_node", args, version, "fable_requires_budget")
        # A discarded escalation does not refund the one-per-run slot. A second
        # Fable means the plan was wrong, per ADR agents/062 decision 3.
        if kind == "fable_escalation" and _fable_ever_created(db, task_id):
            return _refuse(db, task, "add_node", args, version, "fable_cap")

        live_cost = sum(node.max_cost_usd for node in live.values())
        if task.budget_usd is not None and live_cost + max_cost_usd > task.budget_usd:
            return _refuse(db, task, "add_node", args, version, "budget_exceeded")

        new_version = version + 1
        db.add(
            SwarmPlanVersion(
                task_id=task_id,
                version=new_version,
                op="add_node",
                author_kind=author_kind,
                author=author,
                change_json=_json(node_fields),
                cause_kind=cause_kind,
                cause_ref=cause_ref,
                stated_reason=stated_reason,
            )
        )
        existing = db.exec(
            select(SwarmPlanNode).where(
                SwarmPlanNode.task_id == task_id,
                SwarmPlanNode.node_key == node_key,
            )
        ).first()
        if existing is None:
            existing = SwarmPlanNode(
                task_id=task_id,
                node_key=node_key,
                kind=kind,
                prompt=prompt,
                model=model,
                deps_json=_json(deps),
                max_cost_usd=max_cost_usd,
                side_effects=side_effects,
                max_attempts=max_attempts,
                turn_timeout_seconds=turn_timeout_seconds,
                created_in_version=new_version,
            )
        else:
            # Keys are unique per task, so a discarded key is re-added by
            # reusing its row, clearing lifecycle tombstones, and advancing its
            # creation version.
            existing.kind = kind
            existing.prompt = prompt
            existing.model = model
            existing.deps_json = _json(deps)
            existing.max_cost_usd = max_cost_usd
            existing.side_effects = side_effects
            existing.max_attempts = max_attempts
            existing.turn_timeout_seconds = turn_timeout_seconds
            existing.created_in_version = new_version
            existing.discarded_in_version = None
            existing.cancelled_in_version = None
            existing.armed_at = None
            existing.base_artifact_sha = None
        db.add(existing)
        detail = "unbudgeted run" if task.budget_usd is None else None
        return _finish(
            db,
            task,
            "add_node",
            args,
            version,
            GraphOp(ok=True, version=new_version, detail=detail),
        )


def discard_node(
    task_id: str,
    node_key: str,
    *,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
    stated_reason: str | None,
    expected_version: int,
    observed_branch_head: str | None = None,
    activities_claim_write: bool = False,
) -> GraphOp:
    """Discard an unarmed live leaf while retaining its historical visibility."""

    args = {
        "node_key": node_key,
        "author_kind": author_kind,
        "author": author,
        "cause_kind": cause_kind,
        "cause_ref": cause_ref,
        "stated_reason": stated_reason,
        "expected_version": expected_version,
        "observed_branch_head": observed_branch_head,
        "activities_claim_write": activities_claim_write,
    }
    with _session() as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        if expected_version != version:
            return _refuse(db, task, "discard_node", args, version, "stale_version")
        live = {node.node_key: node for node in _visible_nodes(db, task_id, version)}
        node = live.get(node_key)
        if node is None:
            return _refuse(db, task, "discard_node", args, version, "unknown_node")
        has_run = db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
            )
        ).first()
        if node.armed_at is not None or has_run is not None:
            return _refuse(db, task, "discard_node", args, version, "armed")
        if (
            node.base_artifact_sha is not None
            and observed_branch_head is not None
            and observed_branch_head != node.base_artifact_sha
        ):
            return _refuse(db, task, "discard_node", args, version, "branch_moved")
        # An agent activity claim can only force refusal. It cannot prove that
        # discard is safe, which preserves the epistemic split in ADR agents/062
        # decision 5.
        if activities_claim_write:
            return _refuse(db, task, "discard_node", args, version, "write_claimed")
        if any(node_key in _node_deps(other) for other in live.values()):
            return _refuse(db, task, "discard_node", args, version, "dependents")

        new_version = version + 1
        db.add(
            SwarmPlanVersion(
                task_id=task_id,
                version=new_version,
                op="discard_node",
                author_kind=author_kind,
                author=author,
                change_json=_json({"node_key": node_key}),
                cause_kind=cause_kind,
                cause_ref=cause_ref,
                stated_reason=stated_reason,
            )
        )
        node.discarded_in_version = new_version
        db.add(node)
        return _finish(
            db,
            task,
            "discard_node",
            args,
            version,
            GraphOp(ok=True, version=new_version),
        )


def admit_dispatch(
    task_id: str,
    node_key: str,
    *,
    session: Session | None = None,
) -> GraphOp:
    """Reserve the next bounded attempt and durably arm its plan node."""

    args = {"node_key": node_key}
    with _session(session) as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        live = {node.node_key: node for node in _visible_nodes(db, task_id, version)}
        node = live.get(node_key)
        if node is None:
            return _refuse(db, task, "admit_dispatch", args, version, "unknown_node")
        runs = list(
            db.exec(
                select(SwarmNodeRun).where(
                    SwarmNodeRun.task_id == task_id,
                    SwarmNodeRun.node_key == node_key,
                )
            ).all()
        )
        attempt_bound = node.max_attempts if node.max_attempts is not None else 2
        # Re-dispatch after escalation does not bypass the bound. The conductor
        # must discard and re-add with a higher bound, which repeats budget
        # admission, per ADR agents/062 open question 6.
        if len(runs) >= attempt_bound:
            return _refuse(
                db, task, "admit_dispatch", args, version, "attempts_exhausted"
            )
        spent = sum(run.cost_usd or 0.0 for run in runs)
        if spent >= node.max_cost_usd:
            return _refuse(
                db, task, "admit_dispatch", args, version, "node_budget_exhausted"
            )

        attempt = len(runs) + 1
        db.add(
            SwarmNodeRun(
                task_id=task_id,
                node_key=node_key,
                attempt=attempt,
                status="admitted",
            )
        )
        if node.armed_at is None:
            node.armed_at = datetime.now(timezone.utc)
            db.add(node)
        return _finish(
            db,
            task,
            "admit_dispatch",
            args,
            version,
            GraphOp(ok=True, version=version, detail=str(attempt)),
        )


def record_dispatch(
    task_id: str,
    node_key: str,
    attempt: int,
    session_id: int,
    base_sha: str,
) -> GraphOp:
    """Mark one admitted ledger attempt as dispatched."""

    args = {
        "node_key": node_key,
        "attempt": attempt,
        "session_id": session_id,
        "base_sha": base_sha,
    }
    with _session() as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        run = db.exec(
            select(SwarmNodeRun).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt == attempt,
            )
        ).first()
        if run is None:
            raise ValueError(f"Unknown swarm node run {task_id}/{node_key}/{attempt}")
        if run.status != "admitted":
            raise ValueError(f"Cannot dispatch swarm node run in status {run.status}")
        run.status = "dispatched"
        run.session_id = session_id
        run.base_sha = base_sha
        db.add(run)
        return _finish(
            db,
            task,
            "record_dispatch",
            args,
            version,
            GraphOp(ok=True, version=version),
        )


def record_outcome(
    task_id: str,
    node_key: str,
    attempt: int,
    status: str,
    cost_usd: float,
    head_sha: str | None,
    outcome_json: str | None,
) -> GraphOp:
    """Record the terminal outcome of one admitted or dispatched attempt."""

    args = {
        "node_key": node_key,
        "attempt": attempt,
        "status": status,
        "cost_usd": cost_usd,
        "head_sha": head_sha,
        "outcome_json": outcome_json,
    }
    with _session() as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        if status not in _TERMINAL_RUN_STATUSES:
            raise ValueError(f"Non-terminal swarm node run status {status}")
        run = db.exec(
            select(SwarmNodeRun).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt == attempt,
            )
        ).first()
        if run is None:
            raise ValueError(f"Unknown swarm node run {task_id}/{node_key}/{attempt}")
        if run.status not in ("admitted", "dispatched"):
            raise ValueError(f"Cannot finish swarm node run in status {run.status}")
        run.status = status
        run.cost_usd = cost_usd
        run.head_sha = head_sha
        run.outcome_json = outcome_json
        run.finished_at = datetime.now(timezone.utc)
        db.add(run)
        return _finish(
            db,
            task,
            "record_outcome",
            args,
            version,
            GraphOp(ok=True, version=version),
        )


def node_runs(task_id: str, node_key: str | None = None) -> list[dict]:
    """Return dispatch-ledger rows ordered by node and attempt."""

    with _session() as db:
        statement = select(SwarmNodeRun).where(SwarmNodeRun.task_id == task_id)
        if node_key is not None:
            statement = statement.where(SwarmNodeRun.node_key == node_key)
        rows = db.exec(
            statement.order_by(SwarmNodeRun.node_key, SwarmNodeRun.attempt)
        ).all()
        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "node_key": row.node_key,
                "attempt": row.attempt,
                "session_id": row.session_id,
                "status": row.status,
                "cost_usd": row.cost_usd,
                "base_sha": row.base_sha,
                "head_sha": row.head_sha,
                "outcome_json": row.outcome_json,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]
