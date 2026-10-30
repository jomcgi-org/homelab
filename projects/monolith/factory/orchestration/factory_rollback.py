"""Default-off rollback evidence and durable intent reservation.

The control-row write lock serializes reservations and the rolling-hour count
in Postgres and file-backed SQLite. Audit rows are the append-only intent
ledger: reserve commits before any external effect. A started effect is never
reissued after an unknown outcome; reconciliation must find its deterministic
branch/PR. No lease expiry grants permission to create another PR.

This module deliberately exposes no alternate merge path. A discovered PR must
be adopted by the ordinary factory review and landing machinery. Reservation,
PR creation, queue acceptance and landed rollback are distinct states.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

ACTOR = "factory:rollback"
SHA = re.compile(r"[0-9a-f]{40}")
SIGNALS = frozenset({"argocd", "kargo_promotion", "revision_health"})
FRESHNESS_SECONDS = 30


def reverse_source(checkout: str, delivery: dict) -> dict:
    """Validate identity then run the same stdlib helper shipped to workers."""
    from factory.orchestration.factory_rollback_source import reverse_source as reverse

    identity(delivery)
    return reverse(checkout, delivery)


def utc(value: object) -> datetime:
    """Normalize database timestamps; malformed observations stay unknown."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("timestamp required")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def identity(delivery: dict) -> str:
    for key in ("repo", "task_id", "application", "chart", "revision"):
        if not isinstance(delivery.get(key), str) or not delivery[key]:
            raise ValueError("rollback delivery identity incomplete")
    if not SHA.fullmatch(delivery.get("merge_commit_sha", "")):
        raise ValueError("rollback merge identity incomplete")
    for key in ("pr_number", "issue_number"):
        if type(delivery.get(key)) is not int or delivery[key] < 1:
            raise ValueError("rollback delivery reference incomplete")
    utc(delivery["merged_at"])
    # One intent for the whole delivery, even if several applications fail.
    return hashlib.sha256(
        json.dumps(
            [delivery["repo"], delivery["task_id"], delivery["merge_commit_sha"]],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def verdict(
    policy: dict, delivery: dict, observations: object, *, now: datetime
) -> dict:
    """Revision-, signal- and time-scoped integration contract.

    Pipeline failures are eligible from the merge until the observation window
    ends. Window start is a persisted successful pipeline observation, never a
    process-local timer. Unknown inputs cannot either verify or fail a rollout.
    A healthy final sample is required after the window; elapsed time alone
    never verifies service health. Callers retain unknown intermediate samples.
    """
    unknown = {"verdict": "unknown", "reason": "rollback_observation_unknown"}
    try:
        key = identity(delivery)
        config = controls.rollback_policy(policy)
        chart = config["charts"].get(delivery["chart"], {})
        window = timedelta(
            minutes=chart.get("window_minutes", config["window_minutes"])
        )
        merged = utc(delivery["merged_at"])
        started = (
            utc(delivery["window_started_at"])
            if delivery.get("window_started_at")
            else None
        )
        clock = utc(now)
        if merged > clock or (started is not None and not merged <= started <= clock):
            return unknown
        if not isinstance(observations, list):
            return unknown
        required = {"argocd"}
        if chart.get("synthetic_signal"):
            required.add("synthetic:" + chart["synthetic_signal"])
        allowed = SIGNALS | required
        healthy = set()
        failures = []
        for sample in observations:
            if not isinstance(sample, dict) or sample.get("available") is not True:
                continue
            if any(
                sample.get(field) != delivery[field]
                for field in (
                    "repo",
                    "task_id",
                    "merge_commit_sha",
                    "application",
                    "chart",
                    "revision",
                )
            ):
                continue
            signal = sample.get("signal")
            if not isinstance(signal, str) or signal not in allowed:
                continue
            try:
                observed = utc(sample.get("observed_at"))
            except (ValueError, TypeError):
                continue
            if not merged <= observed <= clock or clock - observed > timedelta(
                seconds=FRESHNESS_SECONDS
            ):
                continue
            if sample.get("outcome") == "failed":
                if started is None or observed <= started + window:
                    failures.append(sample)
            elif sample.get("outcome") == "healthy":
                healthy.add(signal)
        if failures:
            return {
                "verdict": "rollout_failed",
                "delivery_key": key,
                "failure": failures[0],
            }
        if required <= healthy:
            if started is not None and clock >= started + window:
                return {"verdict": "rollout_verified", "delivery_key": key}
            return {"verdict": "rollout_observing", "delivery_key": key}
        return unknown
    except (KeyError, ValueError, TypeError, OverflowError):
        return unknown


def _events(db, key: str, task_id: str, application=None) -> dict[str, dict]:
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action.in_(
                (
                    "rollback_reserved",
                    "rollback_pr_started",
                    "rollback_pr_observed",
                    "rollback_landed",
                    "rollback_issue_reopened",
                    "rollout_failed",
                    "rollback_window_started",
                    "rollback_window_verified",
                )
            ),
            FactoryAudit.task_id == task_id,
        )
        .order_by(FactoryAudit.id)
    ).all()
    result = {}
    for row in rows:
        detail = json.loads(row.detail_json)
        if detail.get("delivery_key") == key:
            if row.action.startswith("rollback_window_"):
                if detail.get("application") != application:
                    continue
            result[row.action] = detail
    return result


def _append(db, delivery: dict, action: str, key: str, now: datetime, /, **detail):
    controls._audit(
        db,
        ACTOR,
        action,
        task_id=delivery["task_id"],
        delivery_key=key,
        repo=delivery["repo"],
        recorded_at=utc(now).isoformat(),
        **detail,
    )


def _observe(db, policy, delivery, observations, now):
    key = identity(delivery)
    events = _events(db, key, delivery["task_id"], delivery["application"])
    if "rollout_failed" in events:
        return {"verdict": "rollout_failed", **events["rollout_failed"]}
    if "rollback_window_verified" in events:
        return {"verdict": "rollout_verified", "delivery_key": key}
    started = events.get("rollback_window_started", {}).get("window_started_at")
    delivery = {
        **delivery,
        "window_started_at": started or delivery.get("window_started_at"),
    }
    result = verdict(policy, delivery, observations, now=now)
    if result["verdict"] == "rollout_failed":
        _append(
            db,
            delivery,
            "rollout_failed",
            key,
            now,
            failure=result["failure"],
            delivery=delivery,
        )
    elif result["verdict"] == "rollout_observing" and not started:
        _append(
            db,
            delivery,
            "rollback_window_started",
            key,
            now,
            application=delivery["application"],
            window_started_at=utc(now).isoformat(),
        )
    elif result["verdict"] == "rollout_verified":
        _append(
            db,
            delivery,
            "rollback_window_verified",
            key,
            now,
            application=delivery["application"],
        )
    return result


def observe_window(policy, delivery, observations, *, now, session=None):
    """Persist the window and its terminal failure, independently of enablement."""
    with controls._locked_session(session) as (db, _control):
        return _observe(db, policy, delivery, observations, now)


def reserve(
    policy: dict, delivery: dict, evidence: dict, *, now: datetime, session=None
) -> dict:
    """Commit at most one intent per delivery before a caller may do effects.

    Disabling rollback stops reservations, while existing reservations remain
    readable. Reservations count against the breaker even when their effects
    are still uncertain. The half-open rolling interval is (now - 1h, now].
    """
    key = identity(delivery)
    clock = utc(now)
    with controls._locked_session(session) as (db, control):
        events = _events(db, key, delivery["task_id"])
        if events.get("rollback_reserved"):
            return {"state": "existing", **events["rollback_reserved"]}
        live = controls.rollback_policy(json.loads(control.policy_json or "{}"))
        judged = _observe(db, {"rollback": live}, delivery, [evidence], clock)
        from factory.orchestration.factory_reverts import is_revert_source

        reverted_prs = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "rollback_pr_observed")
        ).all()
        if (
            delivery.get("is_revert") is True
            or is_revert_source(delivery)
            or any(
                (detail := json.loads(row.detail_json)).get("repo") == delivery["repo"]
                and detail.get("pr_number") == delivery["pr_number"]
                for row in reverted_prs
            )
        ):
            return {"state": "needs_human", "reason": "automatic_revert_of_revert"}
        if (
            not live["enabled"]
            or not controls.rollback_policy(policy)["enabled"]
            or control.state != "enabled"
        ):
            return {"state": "disabled"}
        # Re-evaluate the raw observation under current policy inside the lock.
        if judged["verdict"] != "rollout_failed":
            return {"state": "unknown"}
        evidence = judged["failure"]
        receipt = controls._receipt(db, delivery["task_id"])
        if (
            receipt is None
            or receipt.repo != delivery["repo"]
            or receipt.issue_number != delivery["issue_number"]
        ):
            return {"state": "ownership_unknown"}
        if (
            receipt.task_paused
            or receipt.cancellation_requested
            or controls.landing_held(receipt)
        ):
            return {"state": "held"}
        merges = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == delivery["task_id"],
                FactoryAudit.action == "merged",
            )
        ).all()
        if not any(
            (detail := json.loads(row.detail_json)).get("pr_number")
            == delivery["pr_number"]
            and detail.get("merge_commit_sha") == delivery["merge_commit_sha"]
            for row in merges
        ):
            return {"state": "merge_unknown"}
        reserved = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "rollback_reserved")
            .order_by(FactoryAudit.id)
        ).all()
        recent = []
        for row in reserved:
            prior = json.loads(row.detail_json)
            if clock - timedelta(hours=1) < utc(prior["recorded_at"]) <= clock:
                recent.append(row)
        if len(recent) >= live["max_per_hour"]:
            opening = f"factory:{recent[-1].id}"
            existing = db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.action == "rollback_breaker_opened"
                )
            ).all()
            if not any(
                json.loads(row.detail_json).get("opening") == opening
                for row in existing
            ):
                _append(
                    db,
                    delivery,
                    "rollback_breaker_opened",
                    key,
                    clock,
                    opening=opening,
                    reason="rolling_hour_limit",
                    needs_human=True,
                    delivery=delivery,
                )
            return {"state": "breaker_open", "opening": opening}
        detail = {
            "branch": "factory/rollback-" + key[:32],
            "delivery": {
                **delivery,
                "merged_at": utc(delivery["merged_at"]).isoformat(),
                "window_started_at": utc(delivery["window_started_at"]).isoformat()
                if delivery.get("window_started_at")
                else None,
            },
            "failure": evidence,
            "failed_at": utc(evidence["observed_at"]).isoformat(),
        }
        _append(db, delivery, "rollback_reserved", key, clock, **detail)
        return {"state": "reserved", "delivery_key": key, **detail}


def begin_pr_creation(delivery: dict, *, now: datetime, session=None) -> bool:
    """A durable one-shot effect fence. False requires read-only reconciliation."""
    key = identity(delivery)
    with controls._locked_session(session) as (db, _control):
        events = _events(db, key, delivery["task_id"])
        if "rollback_reserved" not in events or "rollback_pr_started" in events:
            return False
        _append(db, delivery, "rollback_pr_started", key, now)
        return True


def observe_pr(delivery: dict, pull: dict, *, now: datetime, session=None) -> dict:
    """Reconcile a deterministic branch; never accept queue/auto-merge as landed.

    Disabled policy intentionally does not stop observing in-flight PRs. A head
    movement invalidates adoption/review evidence and must be reviewed again by
    the ordinary landing owner. No source or queue mutations occur here.
    """
    key = identity(delivery)
    with controls._locked_session(session) as (db, _control):
        events = _events(db, key, delivery["task_id"])
        intent = events.get("rollback_reserved")
        if not intent or "rollback_pr_started" not in events:
            return {"state": "unreserved"}
        head = pull.get("head") or {}
        body = pull.get("body")
        if (
            head.get("ref") != intent["branch"]
            or (pull.get("base") or {}).get("ref") != "main"
            or (head.get("repo") or {}).get("full_name") != delivery["repo"]
            or not SHA.fullmatch(head.get("sha", ""))
            or type(pull.get("number")) is not int
            or pull["number"] < 1
            or not isinstance(body, str)
            or body.splitlines().count(f"Reverts #{delivery['pr_number']}") != 1
            or not re.fullmatch(
                r"revert\([a-z][a-z0-9_-]*\): .+", str(pull.get("title", ""))
            )
        ):
            return {"state": "identity_unknown"}
        previous = events.get("rollback_pr_observed")
        if previous and previous["pr_number"] != pull["number"]:
            return {"state": "duplicate_pr_requires_person"}
        moved = previous is not None and previous["head_sha"] != head["sha"]
        if previous is None or moved:
            _append(
                db,
                delivery,
                "rollback_pr_observed",
                key,
                now,
                pr_number=pull["number"],
                head_sha=head["sha"],
                review_invalidated=moved,
            )
        if pull.get("merged") is not True:
            return {
                "state": "head_moved" if moved else "review_and_queue_pending",
                "pr_number": pull["number"],
            }
        if not SHA.fullmatch(pull.get("merge_commit_sha", "")):
            return {"state": "merge_unknown"}
        try:
            landed = utc(pull["merged_at"])
        except (KeyError, TypeError, ValueError):
            return {"state": "merge_unknown"}
        failed = utc(intent["failed_at"])
        if not failed <= landed <= utc(now):
            return {"state": "merge_unknown"}
        if "rollback_landed" not in events:
            _append(
                db,
                delivery,
                "rollback_landed",
                key,
                now,
                pr_number=pull["number"],
                merge_commit_sha=pull["merge_commit_sha"],
                failed_at=failed.isoformat(),
                landed_at=landed.isoformat(),
                elapsed_seconds=(landed - failed).total_seconds(),
            )
        return {"state": "rollback_landed", "pr_number": pull["number"]}
