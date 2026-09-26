"""Decide whether an ArgoCD Application has rolled out, from its object alone.

Pure: given a raw Application object (as the Kubernetes API returns it) and an
optional expected revision, return a bounded verdict. Both MCP surfaces call
this, the private monolith surface through ``cluster.kubernetes`` and the
agents tier through its restricted client, so every agent that asks gets the
same answer from the same rules.

ArgoCD already aggregates what "rolled out" means: a Deployment is Healthy only
once its new ReplicaSet is fully available, and the Application's health rolls
up every managed resource. So the verdict reads the Application's own status
rather than walking workloads. Two ArgoCD subtleties shape the rules:

- ``status.sync.revision(s)`` is the revision ArgoCD last *compared against*
  (the target), which moves as soon as it refreshes. While the app is Synced
  the target is live, even when no sync ran for it (a new main commit that
  renders identical manifests leaves a git-tracked app Synced with no
  operation). While it is not Synced, what is live is the last successful
  sync's ``operationState.syncResult`` or, failing that, ``status.history[-1]``.
- ``operationState`` is the *last* operation, for whatever revision it ran. A
  failure only counts when it ran for the current target; a revert that lands
  already in sync leaves the old failure behind.

Verdicts:
  verified     target applied, synced, healthy (or deliberately suspended), no
               error condition, and (if given) the expected revision reached
  in_progress  still converging: out of sync, progressing, resources not yet
               created, an operation running, or the expected revision not
               reached yet
  failed       degraded, resources missing after sync, the current
               operation failed, or an ArgoCD error condition is set
"""

from __future__ import annotations

import re
from typing import Any

VERIFIED = "verified"
IN_PROGRESS = "in_progress"
FAILED = "failed"

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_UNHEALTHY_LIMIT = 10
_MESSAGE_LIMIT = 300


class RevisionMismatch(ValueError):
    """The expected revision is a different kind from what the app deploys."""


def _semver(value: str | None) -> tuple[int, ...] | None:
    match = _SEMVER.match(value or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _is_sha(value: str | None) -> bool:
    return bool(value) and _SHA.match(value.lower()) is not None


def _sources(app: dict[str, Any]) -> list[dict[str, Any]]:
    spec = app.get("spec") or {}
    if isinstance(spec.get("sources"), list):
        return [s for s in spec["sources"] if isinstance(s, dict)]
    return [spec["source"]] if isinstance(spec.get("source"), dict) else []


def _revs(block: dict[str, Any] | None) -> list[str]:
    """`revisions` for multi-source apps, `revision` for single-source."""
    block = block or {}
    if isinstance(block.get("revisions"), list):
        return [str(r) for r in block["revisions"]]
    return [str(block["revision"])] if block.get("revision") else []


def _primary_index(sources: list[dict[str, Any]]) -> int:
    """The source that carries the workload: the chart, else the first."""
    for i, source in enumerate(sources):
        if source.get("chart"):
            return i
    return 0


def _at(revisions: list[str], idx: int) -> str | None:
    return revisions[idx] if idx < len(revisions) else None


def _same(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    if _is_sha(a) and _is_sha(b):
        a, b = a.lower(), b.lower()
        return a.startswith(b) or b.startswith(a)
    return a == b


def _applied(status: dict[str, Any]) -> list[str]:
    operation = status.get("operationState") or {}
    if operation.get("phase") == "Succeeded":
        revisions = _revs(operation.get("syncResult"))
        if revisions:
            return revisions
    history = status.get("history") or []
    if history and isinstance(history[-1], dict):
        return _revs(history[-1])
    return []


def _operation_revisions(operation: dict[str, Any]) -> list[str]:
    return _revs(operation.get("syncResult")) or _revs(
        (operation.get("operation") or {}).get("sync")
    )


def _clip(value: Any, limit: int = _MESSAGE_LIMIT) -> str | None:
    text = str(value or "")[:limit]
    return text or None


def verdict(
    app: dict[str, Any], *, expected_revision: str | None = None
) -> dict[str, Any]:
    """A bounded, model-friendly rollout verdict for one Application.

    Raises :class:`RevisionMismatch` when ``expected_revision`` is a chart
    version and the app deploys git commits, or the reverse, since that can
    never be satisfied.
    """
    status = app.get("status") or {}
    sources = _sources(app)
    idx = _primary_index(sources)
    target_revisions = _revs(status.get("sync"))
    target = _at(target_revisions, idx)
    applied = _at(_applied(status), idx)
    requested = sources[idx].get("targetRevision") if sources else None

    if expected_revision:
        comparable = target or applied
        if comparable and (
            (_semver(expected_revision) and _is_sha(comparable))
            or (_is_sha(expected_revision) and _semver(comparable))
        ):
            raise RevisionMismatch(
                f"expected_revision {expected_revision!r} cannot be compared with "
                f"this app's revisions (it deploys {comparable!r}); pass a "
                "chart version for chart apps and a commit sha for git apps"
            )

    checks: list[dict[str, Any]] = []

    def check(name: str, state: str, detail: str) -> None:
        checks.append({"name": name, "state": state, "detail": detail})

    sync_status = (status.get("sync") or {}).get("status") or "Unknown"
    synced = sync_status == "Synced"
    check(
        "sync",
        VERIFIED if synced else IN_PROGRESS,
        f"sync status is {sync_status}",
    )

    operation = status.get("operationState") or {}
    phase = operation.get("phase")
    running = phase in {"Running", "Terminating"}
    if running:
        check("operation", IN_PROGRESS, f"operation {phase}")
    elif phase in {"Failed", "Error"}:
        ran_for = _at(_operation_revisions(operation), idx)
        message = _clip(operation.get("message"))
        if ran_for is None or _same(ran_for, target):
            check("operation", FAILED, f"operation {phase} for {ran_for}: {message}")
        else:
            check(
                "operation",
                VERIFIED,
                f"an earlier operation {phase} for {ran_for}, not the current target",
            )
    elif phase:
        check("operation", VERIFIED, f"last operation {phase}")

    health_status = (status.get("health") or {}).get("status") or "Unknown"
    if health_status == "Healthy":
        health_state, note = VERIFIED, ""
    elif health_status == "Suspended":
        health_state, note = VERIFIED, " (deliberately suspended resources)"
    elif health_status == "Degraded":
        health_state, note = FAILED, ""
    elif health_status == "Missing":
        # Resources not created yet mid-sync are normal; still missing once
        # synced with nothing running means they will not appear.
        converged = synced and not running
        health_state = FAILED if converged else IN_PROGRESS
        note = "" if converged else " (not created yet)"
    else:
        health_state, note = IN_PROGRESS, ""
    check("health", health_state, f"health is {health_status}{note}")

    errors = [
        condition
        for condition in status.get("conditions") or []
        if isinstance(condition, dict)
        and str(condition.get("type", "")).endswith("Error")
    ]
    if errors:
        first = errors[0]
        check(
            "conditions",
            FAILED,
            f"{first.get('type')}: {_clip(first.get('message'))}",
        )

    if _semver(requested) and target is not None and requested != target:
        check(
            "refresh",
            IN_PROGRESS,
            f"requested {requested} but ArgoCD still targets {target}",
        )

    live = target if synced else applied

    if expected_revision:
        have = _semver(live)
        want = _semver(expected_revision)
        if want and have:
            reached = have >= want
            detail = f"expected at least {expected_revision}, live {live}"
        else:
            reached = _same(live, expected_revision)
            detail = f"expected commit {expected_revision}, live {live}"
        check("expected_revision", VERIFIED if reached else IN_PROGRESS, detail)

    unhealthy = [
        {
            key: resource.get(key)
            for key in ("kind", "namespace", "name")
            if resource.get(key)
        }
        | {
            "health": (resource.get("health") or {}).get("status"),
            "message": _clip((resource.get("health") or {}).get("message"), 200),
        }
        for resource in status.get("resources") or []
        if isinstance(resource, dict)
        and (resource.get("health") or {}).get("status")
        not in (None, "Healthy", "Suspended")
    ]

    states = {c["state"] for c in checks}
    if FAILED in states:
        overall = FAILED
    elif IN_PROGRESS in states or not checks:
        overall = IN_PROGRESS
    else:
        overall = VERIFIED
    summary = status.get("summary") or {}
    return {
        "app": (app.get("metadata") or {}).get("name"),
        "verdict": overall,
        "requested_revision": requested,
        "target_revision": target,
        "live_revision": live,
        "last_applied_revision": applied,
        "expected_revision": expected_revision,
        "checks": checks,
        "unhealthy_resources": unhealthy[:_UNHEALTHY_LIMIT],
        "unhealthy_resource_count": len(unhealthy),
        "images": list(summary.get("images") or [])[:32],
        "reconciled_at": status.get("reconciledAt"),
    }
