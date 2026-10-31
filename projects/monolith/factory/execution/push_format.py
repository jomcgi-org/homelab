"""Source-role push contracts and replay-safe prevented-drift evidence."""

import json
import re


def contract(row, db=None):
    key = row.node_key or ""
    source = key.startswith(("implement_", "integrate_")) or re.fullmatch(
        r"correct_[0-9]+", key
    )
    if (
        not source
        or key.startswith("implement_browser_capture_")
        or row.repo != "jomcgi-org/homelab"
    ):
        return None
    match = re.fullmatch(
        r"factory:([A-Za-z0-9_.-]+):([^:]+):([0-9]+)", row.local_session_id
    )
    if not match or match[2] != key or int(match[3]) != row.node_attempt:
        raise ValueError(
            "source push contract requires the exact factory node identity"
        )
    # AgentSession.branch is where hydration starts. New tasks hydrate main;
    # fan-out nodes hydrate the task branch but push their own node branch.
    # Only the admitted immutable pin assigns the push destination, including
    # explicitly adopted branches outside factory/.
    from contextlib import nullcontext
    from sqlmodel import Session, select
    from core.db import get_engine
    from factory.orchestration.models import SwarmNodeRun

    with nullcontext(db) if db is not None else Session(get_engine()) as session:
        run = session.exec(
            select(SwarmNodeRun).where(
                SwarmNodeRun.task_id == match[1],
                SwarmNodeRun.node_key == key,
                SwarmNodeRun.attempt == row.node_attempt,
            )
        ).first()
        # The first turn can dispatch before graph binding commits the session.
        if (
            run is None
            or run.dispatch_key != row.workflow_id
            or run.session_id not in (None, row.id)
        ):
            raise ValueError(
                "source push contract requires the exact admitted dispatch"
            )
        pin = json.loads(run.pin_json) if run.pin_json else {}
    branch = pin.get("branch")
    if (
        pin.get("repo") != row.repo
        or pin.get("hydration_branch", branch) != row.branch
        or not isinstance(branch, str)
        or not branch
        or branch in {"main", "master"}
    ):
        raise ValueError("source push contract requires an exact admitted push branch")
    return {
        "task_id": match[1],
        "node_key": key,
        "branch": branch,
        "base_ref": "refs/remotes/origin/main",
    }


def record(db, row, value):
    """Called inside the existing result transaction; no workflow checkpoint."""
    if value is None:
        return
    expected = contract(row, db)
    if expected is None:
        return

    from sqlmodel import select
    from factory.orchestration.factory_controls import _audit
    from factory.orchestration.factory_models import FactoryAudit
    from factory.orchestration.models import SwarmTask

    reason = None
    if not isinstance(value, dict) or any(
        value.get(k) != v for k, v in expected.items()
    ):
        reason = "push-format result identity mismatch"
    else:
        paths = value.get("paths")
        if (
            value.get("outcome") != "drift_blocked"
            or not isinstance(paths, list)
            or not paths
            or len(paths) > 10000
            or type(value.get("file_count")) is not int
            or any(
                not isinstance(p, str)
                or not p
                or p.startswith("/")
                or ".." in p.split("/")
                for p in paths
            )
            or value.get("file_count") != len(set(paths))
        ):
            reason = "invalid prevented-drift result"
    if reason is not None:
        # Guest evidence cannot prevent saving the completed turn. Keep the
        # rejection bounded and attributed only to the admitted server identity.
        _audit(
            db,
            "factory-result",
            "format_drift_evidence_rejected",
            task_id=expected["task_id"],
            node_key=expected["node_key"],
            session_id=row.id,
            reason=reason,
        )
        return

    # Serialize all sessions/attempts for this task before reading the audit
    # fence. The result and new-path saving commit together.
    task = db.exec(
        select(SwarmTask).where(SwarmTask.id == expected["task_id"]).with_for_update()
    ).first()
    if task is None:
        raise ValueError("push-format task no longer exists")
    prior = db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.task_id == expected["task_id"],
            FactoryAudit.action == "format_drift_prevented",
        )
    ).all()
    seen = set()
    for raw in prior:
        event = json.loads(raw)
        if event.get("node_key") == expected["node_key"]:
            seen.update(event.get("paths", []))
    new_paths = sorted(set(paths) - seen)
    if new_paths:
        _audit(
            db,
            "factory-result",
            "format_drift_prevented",
            task_id=expected["task_id"],
            node_key=expected["node_key"],
            session_id=row.id,
            paths=new_paths,
            file_count=len(new_paths),
        )
