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
import heapq
import json
import math
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
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "escalated", "cancelled")
MAX_ATTEMPTS = 10
MAX_PLAN_EDITS = 20
MAX_TURN_TIMEOUT_SECONDS = 43200
_CONTEXT_FIELDS = frozenset(
    (
        "repo",
        "branch",
        "workflow_id",
        "artifact_path",
        "artifact_schema",
        "hydration_branch",
        "retry_context",
        "task_deadline_at",
    )
)


@dataclass
class GraphOp:
    ok: bool
    version: int | None = None
    refusal_code: str | None = None
    detail: str | None = None
    attempt: int | None = None
    pin: dict | None = None


class _PlanRejected(Exception):
    """One edit of an atomic batch was refused, so none of the batch applies."""

    def __init__(self, index: int, op: GraphOp) -> None:
        super().__init__(f"plan edit {index} refused: {op.refusal_code}")
        self.index = index
        self.op = op


@contextmanager
def _session(session: Session | None = None) -> Iterator[Session]:
    if session is not None:
        yield session
        return
    with Session(get_engine()) as owned_session:
        yield owned_session
        owned_session.commit()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _audit_value(value: Any) -> Any:
    """Keep refused nonfinite inputs auditable as valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _audit_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def _valid_cost(value: Any, *, zero: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and (value >= 0 if zero else value > 0)
    except OverflowError:
        return False


def _valid_bound(value: Any, ceiling: int) -> bool:
    return type(value) is int and 0 < value <= ceiling


def _bounds_error(node: SwarmPlanNode | dict) -> str | None:
    values = node if isinstance(node, dict) else vars(node)
    if not _valid_cost(values.get("max_cost_usd")):
        return "invalid_node_budget"
    if not _valid_bound(values.get("max_attempts"), MAX_ATTEMPTS):
        return "invalid_max_attempts"
    if not _valid_bound(values.get("turn_timeout_seconds"), MAX_TURN_TIMEOUT_SECONDS):
        return "invalid_turn_timeout"
    return None


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
        if version is not None and version < _current_version(db, task_id):
            return _historical_nodes(db, task_id, selected_version)
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


def _historical_nodes(db: Session, task_id: str, version: int) -> list[dict]:
    """Recover earlier incarnations even when a discarded key was re-added.

    Bootstrap nodes predate semantic add operations, so retain their row as a
    fallback. Every semantic add has a complete immutable field snapshot in
    the version ledger. A later re-add must never rewrite that snapshot.
    """
    rows = list(
        db.exec(
            select(SwarmPlanNode)
            .where(SwarmPlanNode.task_id == task_id)
            .order_by(SwarmPlanNode.id)
        ).all()
    )
    changes = list(
        db.exec(
            select(SwarmPlanVersion)
            .where(SwarmPlanVersion.task_id == task_id)
            .order_by(SwarmPlanVersion.version)
        ).all()
    )
    incarnations: dict[tuple[str, int], dict] = {}
    for node in rows:
        incarnations[(node.node_key, node.created_in_version)] = {
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
    for change in changes:
        if change.op != "add_node":
            continue
        fields = json.loads(change.change_json)
        key = (fields["node_key"], change.version)
        if key not in incarnations:
            incarnations[key] = {
                **fields,
                "created_in_version": change.version,
                "discarded_in_version": None,
                "cancelled_in_version": None,
                "armed_at": None,
                "base_artifact_sha": None,
            }
    for change in changes:
        if change.op != "discard_node":
            continue
        payload = json.loads(change.change_json)
        node_key = payload["node_key"]
        snapshot = payload.get("snapshot")
        if snapshot is not None:
            key = (node_key, snapshot["created_in_version"])
            incarnations.setdefault(key, snapshot)
        candidates = [
            key
            for key in incarnations
            if key[0] == node_key and key[1] < change.version
        ]
        if candidates:
            key = max(candidates, key=lambda item: item[1])
            incarnations[key]["discarded_in_version"] = change.version
    return [
        node
        for _, node in sorted(incarnations.items(), key=lambda item: item[0][1])
        if node["created_in_version"] <= version
        and (
            node["discarded_in_version"] is None
            or node["discarded_in_version"] > version
        )
        and (
            node["cancelled_in_version"] is None
            or node["cancelled_in_version"] > version
        )
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
            args_json=_json(_audit_value(args)),
            outcome="applied" if result.ok else "refused",
            refusal_code=result.refusal_code,
            version_before=version_before,
            version_after=result.version,
        )
    )
    # The caller owns the transaction when a Session is supplied. Owned
    # sessions commit at the context boundary, including this audit row.
    db.flush()
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
    session: Session | None = None,
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
    with _session(session) as db:
        return _add_node_locked(
            db,
            _lock_task(db, task_id),
            task_id,
            args,
            node_fields,
            expected_version,
            author_kind,
            author,
            cause_kind,
            cause_ref,
            stated_reason,
        )


def _add_node_locked(
    db: Session,
    task: SwarmTask,
    task_id: str,
    args: dict[str, Any],
    node_fields: dict[str, Any],
    expected_version: int,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
    stated_reason: str | None,
) -> GraphOp:
    """Validate and apply one add against an already locked task row."""
    node_key = node_fields["node_key"]
    kind = node_fields["kind"]
    prompt = node_fields["prompt"]
    model = node_fields["model"]
    deps = node_fields["deps"]
    max_cost_usd = node_fields["max_cost_usd"]
    side_effects = node_fields["side_effects"]
    max_attempts = node_fields["max_attempts"]
    turn_timeout_seconds = node_fields["turn_timeout_seconds"]
    version = _current_version(db, task_id)
    if expected_version != version:
        return _refuse(db, task, "add_node", args, version, "stale_version")
    error = _bounds_error(node_fields)
    if error:
        return _refuse(db, task, "add_node", args, version, error)
    if task.budget_usd is not None and not _valid_cost(task.budget_usd):
        return _refuse(db, task, "add_node", args, version, "invalid_task_budget")

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

    existing = db.exec(
        select(SwarmPlanNode).where(
            SwarmPlanNode.task_id == task_id,
            SwarmPlanNode.node_key == node_key,
        )
    ).first()
    # Legacy runs without pinned reservations may still charge this row's
    # ceiling. Reusing a hidden key must not rewrite their unknown cost.
    if (
        existing is not None
        and existing.max_cost_usd != max_cost_usd
        and any(
            run.node_key == node_key
            and run.reserved_cost_usd is None
            and _accounting_basis(run) not in SETTLED_ACCOUNTING_BASES
            for run in _runs(db, task_id)
        )
    ):
        return _refuse(
            db, task, "add_node", args, version, "legacy_reservation_conflict"
        )
    if task.budget_usd is not None and (
        _planned_cost(db, task_id, list(live.values())) + max_cost_usd > task.budget_usd
    ):
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
    session: Session | None = None,
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
    with _session(session) as db:
        return _discard_node_locked(
            db,
            _lock_task(db, task_id),
            task_id,
            node_key,
            args,
            expected_version,
            author_kind,
            author,
            cause_kind,
            cause_ref,
            stated_reason,
            observed_branch_head,
        )


def _discard_node_locked(
    db: Session,
    task: SwarmTask,
    task_id: str,
    node_key: str,
    args: dict[str, Any],
    expected_version: int,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
    stated_reason: str | None,
    observed_branch_head: str | None,
) -> GraphOp:
    """Validate and apply one discard against an already locked task row."""
    activities_claim_write = bool(args.get("activities_claim_write"))
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
            change_json=_json(
                {
                    "node_key": node_key,
                    "snapshot": next(
                        item
                        for item in load_graph(task_id, session=db)
                        if item["node_key"] == node_key
                    ),
                }
            ),
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


def _plan_edit_args(edit: Any) -> Any:
    """Record every edit field except the prompt body, which is unbounded."""
    if not isinstance(edit, dict):
        return _audit_value(edit)
    recorded = {key: value for key, value in edit.items() if key != "prompt"}
    prompt = edit.get("prompt")
    if isinstance(prompt, str):
        recorded["prompt_chars"] = len(prompt)
    return recorded


def _resolve_batch_deps(edits: list[dict], live: set[str]) -> list[dict]:
    """Let a plan name a dependency by the key its author wrote.

    A caller may rewrite an edit's ``node_key`` before it gets here, as the
    factory does when it prefixes a decision key with its role, and it cannot
    rewrite the deps that sibling edits already wrote. Without this a plan that
    says ``fix`` where the server stored ``implement_fix`` would refuse whole
    for a spelling. An alias resolves only when nothing live and nothing else
    in the batch already answers to that exact name, and an ambiguous alias
    resolves to nothing rather than to a guess.
    """
    final = {edit["node_key"] for edit in edits if edit["op"] == "add_node"}
    alias: dict[str, str | None] = {}
    for edit in edits:
        raw = edit.get("raw_node_key")
        if (
            edit["op"] == "add_node"
            and isinstance(raw, str)
            and raw
            and raw != edit["node_key"]
            and raw not in live
            and raw not in final
        ):
            alias[raw] = None if raw in alias else edit["node_key"]
    if not alias:
        return list(edits)
    resolved = []
    for edit in edits:
        deps = edit.get("deps")
        if edit["op"] != "add_node" or not isinstance(deps, list):
            resolved.append(edit)
            continue
        resolved.append(
            {
                **edit,
                "deps": [
                    alias[dep]
                    if dep not in live and dep not in final and alias.get(dep)
                    else dep
                    for dep in deps
                ],
            }
        )
    return resolved


def _order_edits(edits: list[dict]) -> list[tuple[int, dict]]:
    """Apply a batch in dependency order, reordering nothing else.

    Each edit commits its own version before the next one runs, so an add
    whose dependency is added later in the same batch would refuse and take
    the plan with it. A stable topological sort accepts either ordering and
    leaves an already ordered plan exactly as written. Edits touching the same
    key keep their relative order, so a discard and its re-add cannot swap. A
    batch whose edits depend on each other circularly cannot be ordered at all,
    so it keeps the author's order and refuses through the ordinary dependency
    check rather than through a new code.
    """
    indexed = list(enumerate(edits))
    producer: dict[str, int] = {}
    for index, edit in indexed:
        if edit["op"] == "add_node":
            producer.setdefault(edit["node_key"], index)
    successors: dict[int, set[int]] = {index: set() for index, _ in indexed}
    indegree = {index: 0 for index, _ in indexed}

    def link(before: int, after: int) -> None:
        if before != after and after not in successors[before]:
            successors[before].add(after)
            indegree[after] += 1

    last_touch: dict[str, int] = {}
    for index, edit in indexed:
        key = edit["node_key"]
        if key in last_touch:
            link(last_touch[key], index)
        last_touch[key] = index
        if edit["op"] != "add_node":
            continue
        for dep in edit.get("deps") or []:
            source = producer.get(dep)
            if source is not None:
                link(source, index)

    available = [index for index, _ in indexed if indegree[index] == 0]
    heapq.heapify(available)
    order: list[int] = []
    while available:
        index = heapq.heappop(available)
        order.append(index)
        for successor in sorted(successors[index]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(available, successor)
    if len(order) != len(indexed):
        return indexed
    return [(index, edits[index]) for index in order]


def _edits_error(edits: Any) -> str | None:
    """Refuse a malformed batch before any row is locked or written."""
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_PLAN_EDITS:
        return "invalid_edits"
    for edit in edits:
        if not isinstance(edit, dict) or edit.get("op") not in (
            "add_node",
            "discard_node",
        ):
            return "invalid_edits"
        if not isinstance(edit.get("node_key"), str) or not edit["node_key"]:
            return "invalid_edits"
        if edit["op"] == "discard_node":
            continue
        if not isinstance(edit.get("prompt"), str) or not isinstance(
            edit.get("kind"), str
        ):
            return "invalid_edits"
        deps = edit.get("deps")
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            return "invalid_edits"
    return None


def apply_edits(
    task_id: str,
    *,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
    expected_version: int,
    edits: list[dict],
) -> GraphOp:
    """Apply an ordered batch of graph edits, or none of them.

    Each edit becomes its own plan version under the shared cause, so history,
    ``_historical_nodes`` and the version ledger read exactly as they do for a
    single edit. The batch chains its own revisions: the caller supplies one
    ``expected_version`` for the first edit and every later edit is checked
    against the revision its predecessor produced.

    This call owns its transaction rather than joining a caller's. A refused
    edit abandons the whole batch before commit, and the refusal itself is
    recorded afterwards in a fresh transaction, so a partially applied plan can
    never reach the graph and a refusal can never be lost with it.
    """
    args = {
        "author_kind": author_kind,
        "author": author,
        "cause_kind": cause_kind,
        "cause_ref": cause_ref,
        "expected_version": expected_version,
        "edits": (
            [_plan_edit_args(edit) for edit in edits]
            if isinstance(edits, list)
            else _audit_value(edits)
        ),
    }
    rejection = _edits_error(edits)
    detail = "plan edits are not a bounded list of graph operations"
    if rejection is None:
        try:
            with Session(get_engine()) as db:
                task = _lock_task(db, task_id)
                live = {
                    node.node_key
                    for node in _visible_nodes(
                        db, task_id, _current_version(db, task_id)
                    )
                }
                version = expected_version
                for index, edit in _order_edits(_resolve_batch_deps(edits, live)):
                    result = _apply_one_edit(
                        db,
                        task,
                        task_id,
                        edit,
                        version,
                        author_kind,
                        author,
                        cause_kind,
                        cause_ref,
                    )
                    if not result.ok:
                        raise _PlanRejected(index, result)
                    version = result.version
                applied = _finish(
                    db,
                    task,
                    "apply_edits",
                    args,
                    expected_version,
                    GraphOp(ok=True, version=version),
                )
                db.commit()
                return applied
        except _PlanRejected as exc:
            # The session closed without a commit, so every edit rolled back.
            rejection = exc.op.refusal_code or "graph_refused"
            detail = (
                f"edit {exc.index} ({edits[exc.index].get('node_key')}): {rejection}"
            )
            if exc.op.detail:
                detail = f"{detail} ({exc.op.detail})"
    with Session(get_engine()) as db:
        task = _lock_task(db, task_id)
        refused = _refuse(
            db,
            task,
            "apply_edits",
            args,
            _current_version(db, task_id),
            rejection,
            detail,
        )
        db.commit()
        return refused


def _apply_one_edit(
    db: Session,
    task: SwarmTask,
    task_id: str,
    edit: dict,
    expected_version: int,
    author_kind: str,
    author: str,
    cause_kind: str,
    cause_ref: str | None,
) -> GraphOp:
    stated_reason = edit.get("stated_reason")
    if edit["op"] == "discard_node":
        args = {
            "node_key": edit["node_key"],
            "author_kind": author_kind,
            "author": author,
            "cause_kind": cause_kind,
            "cause_ref": cause_ref,
            "stated_reason": stated_reason,
            "expected_version": expected_version,
            "observed_branch_head": edit.get("observed_branch_head"),
            "activities_claim_write": bool(edit.get("activities_claim_write")),
        }
        return _discard_node_locked(
            db,
            task,
            task_id,
            edit["node_key"],
            args,
            expected_version,
            author_kind,
            author,
            cause_kind,
            cause_ref,
            stated_reason,
            edit.get("observed_branch_head"),
        )
    node_fields = {
        "node_key": edit["node_key"],
        "kind": edit["kind"],
        "prompt": edit["prompt"],
        "model": edit.get("model"),
        "deps": list(edit["deps"]),
        "max_cost_usd": edit.get("max_cost_usd"),
        "side_effects": bool(edit.get("side_effects")),
        "max_attempts": edit.get("max_attempts"),
        "turn_timeout_seconds": edit.get("turn_timeout_seconds"),
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
    return _add_node_locked(
        db,
        task,
        task_id,
        args,
        node_fields,
        expected_version,
        author_kind,
        author,
        cause_kind,
        cause_ref,
        stated_reason,
    )


def _runs(db: Session, task_id: str) -> list[SwarmNodeRun]:
    return list(
        db.exec(
            select(SwarmNodeRun)
            .where(SwarmNodeRun.task_id == task_id)
            .order_by(SwarmNodeRun.node_key, SwarmNodeRun.attempt)
        ).all()
    )


def _node_budgets(db: Session, task_id: str) -> dict[str, float]:
    return {
        node.node_key: node.max_cost_usd
        for node in db.exec(
            select(SwarmPlanNode).where(SwarmPlanNode.task_id == task_id)
        ).all()
    }


def _reservation(run: SwarmNodeRun, budgets: dict[str, float]) -> float:
    # Legacy ledger rows have no pin. Preserve their entire node ceiling until
    # measured cost is available, never silently refund an unknown attempt.
    value = run.reserved_cost_usd
    if value is None:
        value = budgets.get(run.node_key)
    if not _valid_cost(value):
        raise ValueError(f"Missing valid reservation for swarm node run {run.id}")
    return float(value)


NO_MODEL_POST_PHASES = frozenset(
    {"never_dispatched", "not_invoked", "lost_before_guest"}
)


def _invocation_phase(run: SwarmNodeRun) -> str | None:
    """Read narrowly scoped proof that a terminal attempt never reached a model."""
    if not run.outcome_json:
        return None
    try:
        outcome = json.loads(run.outcome_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(outcome, dict):
        return None
    phase = outcome.get("invocation_phase")
    if isinstance(phase, str):
        return phase
    recovery = outcome.get("recovery")
    if isinstance(recovery, dict) and isinstance(
        recovery.get("invocation_phase"), str
    ):
        return recovery["invocation_phase"]
    # Typed reconciliation proofs are attached under the phase they prove.
    # Do not search arbitrary nested history: a safe phase in a previous
    # outcome must not refund a later attempt that may have reached the model.
    for expected in NO_MODEL_POST_PHASES:
        proof = outcome.get(expected)
        if (
            isinstance(proof, dict)
            and proof.get("invocation_phase") == expected
        ):
            return expected
    return None


def _accounted_cost(run: SwarmNodeRun, budgets: dict[str, float]) -> float:
    measured = run.cost_usd if _valid_cost(run.cost_usd, zero=True) else None
    if run.status in TERMINAL_RUN_STATUSES and measured is not None:
        return float(measured)
    if (
        run.status in TERMINAL_RUN_STATUSES
        and _invocation_phase(run) in NO_MODEL_POST_PHASES
    ):
        return 0.0
    return max(_reservation(run, budgets), float(measured or 0.0))


def _planned_cost(db: Session, task_id: str, live: list[SwarmPlanNode]) -> float:
    budgets = _node_budgets(db, task_id)
    accounted: dict[str, float] = {}
    succeeded: set[str] = set()
    # History remains charged even when its node is no longer visible.
    for run in _runs(db, task_id):
        accounted[run.node_key] = accounted.get(run.node_key, 0.0) + _accounted_cost(
            run, budgets
        )
        if run.status == "succeeded":
            succeeded.add(run.node_key)
    # Successful nodes cannot dispatch again. Other visible nodes retain their
    # unused ceiling, including failed nodes whose retry may become runnable.
    remaining = sum(
        max(0.0, node.max_cost_usd - accounted.get(node.node_key, 0.0))
        for node in live
        if node.node_key not in succeeded
    )
    return sum(accounted.values()) + remaining


def budget_snapshot(task_id: str, *, session: Session | None = None) -> dict:
    """Read the same conservative accounting and planned allocation as admission."""
    with _session(session) as db:
        task = db.get(SwarmTask, task_id)
        if task is None:
            raise ValueError("task not found")
        version = _current_version(db, task_id)
        runs = _runs(db, task_id)
        budgets = _node_budgets(db, task_id)
        accounted = sum(_accounted_cost(run, budgets) for run in runs)
        active = [run for run in runs if run.status not in TERMINAL_RUN_STATUSES]
        active_cost = sum(_accounted_cost(run, budgets) for run in active)
        planned = _planned_cost(db, task_id, _visible_nodes(db, task_id, version))
        return {
            "graph_version": version,
            "task_budget_usd": task.budget_usd,
            "accounted_cost_usd": accounted,
            "settled_accounted_cost_usd": accounted - active_cost,
            "active_accounted_cost_usd": active_cost,
            "planned_cost_usd": planned,
            "unallocated_cost_usd": (
                max(0.0, task.budget_usd - planned)
                if task.budget_usd is not None
                else None
            ),
            "active_attempts": len(active),
            "uncertain_attempts": sum(run.status == "uncertain" for run in active),
        }


# A settled attempt charges its own cost rather than its whole reservation when
# measured, list priced, or proven to have stopped before any model POST.
SETTLED_ACCOUNTING_BASES = ("reported", "list_priced", "no_model_post")


def _reported_cost_basis(run: SwarmNodeRun) -> str:
    """Read the settlement evidence the node result recorded, defaulting to measured."""
    if not run.outcome_json:
        return "reported"
    try:
        outcome = json.loads(run.outcome_json)
    except (TypeError, ValueError):
        return "reported"
    if not isinstance(outcome, dict):
        return "reported"
    return "list_priced" if outcome.get("cost_basis") == "list" else "reported"


def _accounting_basis(run: SwarmNodeRun) -> str:
    if run.status not in TERMINAL_RUN_STATUSES:
        return "active_reservation"
    if _valid_cost(run.cost_usd, zero=True):
        return _reported_cost_basis(run)
    if _invocation_phase(run) in NO_MODEL_POST_PHASES:
        return "no_model_post"
    return "reserved_unknown_cost"


def admit_dispatch(
    task_id: str,
    node_key: str,
    *,
    dispatch_key: str | None = None,
    execution_context: dict | None = None,
    model: str | None = None,
    session: Session | None = None,
) -> GraphOp:
    """Atomically reserve one bounded attempt, or replay its immutable pin.

    ``dispatch_key`` belongs to the admission command, not to the current plan
    revision. Reconciliation must reuse an existing ledger pin after a lost
    workflow checkpoint. A new command cannot bypass an active reservation.

    ``model`` substitutes the model for THIS attempt, which is how a review
    falls back to a cheaper reviewer without rewriting the plan. The node keeps
    the model the plan asked for and the pin records what actually ran, so
    evidence reads the pin. It is a dispatch parameter rather than part of the
    execution context on purpose: the context is what the guest receives and
    what replay identity is compared on, and a replay must return its original
    pin whatever the dispatcher would choose now.
    """
    context = {} if execution_context is None else execution_context
    args = {
        "node_key": node_key,
        "dispatch_key": dispatch_key,
        "execution_context": context,
        "model": model,
    }
    with _session(session) as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)

        def refuse(code: str, detail: str | None = None) -> GraphOp:
            return _refuse(db, task, "admit_dispatch", args, version, code, detail)

        if dispatch_key is not None and (
            not isinstance(dispatch_key, str)
            or not dispatch_key.strip()
            or len(dispatch_key) > 256
        ):
            return refuse("invalid_dispatch_key")
        if not isinstance(context, dict) or set(context) - _CONTEXT_FIELDS:
            return refuse("invalid_execution_context")
        try:
            context = json.loads(_json(context))
        except (TypeError, ValueError):
            return refuse("invalid_execution_context")
        all_runs = _runs(db, task_id)
        if dispatch_key is not None:
            existing = next(
                (run for run in all_runs if run.dispatch_key == dispatch_key), None
            )
            if existing is not None:
                pin = json.loads(existing.pin_json) if existing.pin_json else None
                if (
                    existing.node_key != node_key
                    or pin is None
                    or {
                        key: value
                        for key, value in pin.items()
                        if key in _CONTEXT_FIELDS
                    }
                    != context
                ):
                    return refuse("dispatch_key_conflict")
                return _finish(
                    db,
                    task,
                    "admit_dispatch",
                    args,
                    version,
                    GraphOp(
                        ok=True,
                        version=version,
                        detail=str(existing.attempt),
                        attempt=existing.attempt,
                        pin=pin,
                    ),
                )
        live = {node.node_key: node for node in _visible_nodes(db, task_id, version)}
        node = live.get(node_key)
        if node is None:
            return refuse("unknown_node")
        error = _bounds_error(node)
        if error:
            return refuse(error)
        if not _valid_cost(task.budget_usd):
            return refuse("invalid_task_budget")
        runs = [run for run in all_runs if run.node_key == node_key]
        if any(run.status == "succeeded" for run in runs):
            return refuse("node_succeeded")
        if any(run.status not in TERMINAL_RUN_STATUSES for run in runs):
            return refuse("active_attempt")
        if len(runs) >= node.max_attempts:
            return refuse("attempts_exhausted")
        for dependency in _node_deps(node):
            if dependency not in live or not any(
                run.node_key == dependency and run.status == "succeeded"
                for run in all_runs
            ):
                return refuse("dependency_not_succeeded", dependency)
        budgets = _node_budgets(db, task_id)
        spent = sum(_accounted_cost(run, budgets) for run in runs)
        remaining = node.max_cost_usd - spent
        if remaining <= 0:
            return refuse("node_budget_exhausted")
        task_spent = sum(_accounted_cost(run, budgets) for run in all_runs)
        if task_spent + remaining > task.budget_usd:
            return refuse("task_budget_exhausted")
        attempt = max((run.attempt for run in runs), default=0) + 1
        pin = {
            **context,
            "task_id": task_id,
            "node_key": node_key,
            "attempt": attempt,
            "prompt": node.prompt,
            "model": model or node.model,
            "max_cost_usd": remaining,
            "max_attempts": node.max_attempts,
            "turn_timeout_seconds": node.turn_timeout_seconds,
        }
        db.add(
            SwarmNodeRun(
                task_id=task_id,
                node_key=node_key,
                attempt=attempt,
                dispatch_key=dispatch_key,
                pin_json=_json(pin),
                reserved_cost_usd=remaining,
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
            GraphOp(
                ok=True, version=version, detail=str(attempt), attempt=attempt, pin=pin
            ),
        )


def _get_run(db: Session, task_id: str, node_key: str, attempt: int) -> SwarmNodeRun:
    run = db.exec(
        select(SwarmNodeRun).where(
            SwarmNodeRun.task_id == task_id,
            SwarmNodeRun.node_key == node_key,
            SwarmNodeRun.attempt == attempt,
        )
    ).first()
    if run is None:
        raise ValueError(f"Unknown swarm node run {task_id}/{node_key}/{attempt}")
    return run


def record_dispatch(
    task_id: str,
    node_key: str,
    attempt: int,
    session_id: int,
    base_sha: str,
    *,
    session: Session | None = None,
) -> GraphOp:
    """Bind the admitted attempt to one session; exact replay is harmless."""
    args = {
        "node_key": node_key,
        "attempt": attempt,
        "session_id": session_id,
        "base_sha": base_sha,
    }
    with _session(session) as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        run = _get_run(db, task_id, node_key, attempt)
        if type(session_id) is not int or session_id <= 0:
            return _refuse(
                db, task, "record_dispatch", args, version, "invalid_session_id"
            )
        if run.session_id is not None:
            if (run.session_id, run.base_sha) != (session_id, base_sha):
                return _refuse(
                    db, task, "record_dispatch", args, version, "dispatch_conflict"
                )
        elif run.status not in ("admitted", "uncertain"):
            return _refuse(
                db, task, "record_dispatch", args, version, "dispatch_conflict"
            )
        else:
            if run.status == "admitted":
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
            GraphOp(ok=True, version=version, attempt=attempt),
        )


def record_outcome(
    task_id: str,
    node_key: str,
    attempt: int,
    status: str,
    cost_usd: float | None,
    head_sha: str | None,
    outcome_json: str | None,
    *,
    session: Session | None = None,
) -> GraphOp:
    """Record observed evidence without reopening terminal execution.

    Unknown execution keeps its reservation and may later be reconciled to a
    terminal result. Unknown usage on completed execution consumes the entire
    reservation while allowing a proven successful dependency to advance.
    """
    args = {
        "node_key": node_key,
        "attempt": attempt,
        "status": status,
        "cost_usd": cost_usd,
        "head_sha": head_sha,
        "outcome_json": outcome_json,
    }
    with _session(session) as db:
        task = _lock_task(db, task_id)
        version = _current_version(db, task_id)
        if status not in (*TERMINAL_RUN_STATUSES, "uncertain"):
            raise ValueError(f"Invalid swarm node run outcome status {status}")
        if cost_usd is not None and not _valid_cost(cost_usd, zero=True):
            return _refuse(db, task, "record_outcome", args, version, "invalid_cost")
        run = _get_run(db, task_id, node_key, attempt)
        evidence = (status, cost_usd, head_sha, outcome_json)
        previous = (run.status, run.cost_usd, run.head_sha, run.outcome_json)
        if previous == evidence:
            return _finish(
                db,
                task,
                "record_outcome",
                args,
                version,
                GraphOp(ok=True, version=version, attempt=attempt),
            )
        # Unknown execution is recorded once and then only reconciled to a
        # terminal result. The single exception is cost arriving where there
        # was none: a workflow can complete and still report unknown execution
        # while carrying real provider spend, and refusing that would leave the
        # attempt with no measured cost to settle against when it does become
        # terminal. It adds evidence without reopening anything.
        priced = (
            run.status == "uncertain"
            and status == "uncertain"
            and run.cost_usd is None
            and cost_usd is not None
        )
        if not priced and (
            run.status in TERMINAL_RUN_STATUSES
            or (run.status == "uncertain" and status == "uncertain")
        ):
            return _refuse(
                db, task, "record_outcome", args, version, "outcome_conflict"
            )
        if run.status not in ("admitted", "dispatched", "uncertain"):
            raise ValueError(f"Cannot finish swarm node run in status {run.status}")
        run.status = status
        run.cost_usd = cost_usd
        run.head_sha = head_sha
        run.outcome_json = outcome_json
        run.finished_at = (
            datetime.now(timezone.utc) if status in TERMINAL_RUN_STATUSES else None
        )
        db.add(run)
        return _finish(
            db,
            task,
            "record_outcome",
            args,
            version,
            GraphOp(ok=True, version=version, attempt=attempt),
        )


def node_runs(
    task_id: str, node_key: str | None = None, *, session: Session | None = None
) -> list[dict]:
    """Return pinned ledger evidence and conservative accounting per attempt."""
    with _session(session) as db:
        budgets = _node_budgets(db, task_id)
        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "node_key": row.node_key,
                "attempt": row.attempt,
                "dispatch_key": row.dispatch_key,
                "pin": json.loads(row.pin_json) if row.pin_json else None,
                "reserved_cost_usd": row.reserved_cost_usd,
                "accounted_cost_usd": _accounted_cost(row, budgets),
                "accounting_basis": _accounting_basis(row),
                "session_id": row.session_id,
                "status": row.status,
                "cost_usd": row.cost_usd,
                "base_sha": row.base_sha,
                "head_sha": row.head_sha,
                "outcome_json": row.outcome_json,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in _runs(db, task_id)
            if node_key is None or row.node_key == node_key
        ]
