"""Bounded, private per-sweep observations. Receipt creation is not admission.

The sweep stages plain observations before the conductor's admission pass.
Finalization takes the factory lock, resolves actual receipt states, and appends
one candidate audit per identity. Replaying finalization is a no-op. An abandoned
pending sweep is finalized before the next sweep; no audit history is rewritten.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, tuple_
from sqlmodel import select

from core.clock import aware
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
)


def latest(db, action):
    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == action)
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()


class Sweep:
    def __init__(self, sweep_id, now):
        self.id = sweep_id
        self.now = now
        self.issues = {}
        self.coverage = {}

    def observe(self, repo, issue, source="github"):
        if not isinstance(issue, dict):
            return
        number = issue.get("number")
        raw_labels = issue.get("labels") or []
        if not isinstance(raw_labels, list):
            return
        labels = [
            label.get("name") if isinstance(label, dict) else label
            for label in raw_labels
        ]
        if (
            type(number) is not int
            or number <= 0
            or "agent-ready" not in [str(label).lower() for label in labels]
            or issue.get("state", "open") != "open"
            or "pull_request" in issue
        ):
            return
        key = (repo, number)
        if key in self.issues and self.issues[key]["source"] == "local":
            return
        self.issues[key] = {
            "repo": repo,
            "issue_number": number,
            "title": str(issue.get("title") or "")[:512],
            "created_at": issue.get("created_at")
            if isinstance(issue.get("created_at"), str)
            else None,
            "source": source,
            "labels": labels,
            "decision": "skipped",
            "reason": "not_selected",
        }

    def reason(self, repo, number, reason):
        row = self.issues.get((repo, number))
        if row is not None:
            row["reason"] = "capacity" if reason == "lane_full" else reason

    def stage(self):
        from factory.orchestration.factory_controls import _audit, _locked_session

        with _locked_session() as (db, _control):
            previous = latest(db, "intake_pending")
            if (
                previous is not None
                and json.loads(previous.detail_json)["sweep_id"] == self.id
            ):
                return
            _audit(
                db,
                "factory:intake",
                "intake_pending",
                sweep_id=self.id,
                observed_at=aware(self.now).isoformat(),
                coverage=self.coverage,
                issues=list(self.issues.values()),
                fresh_for_seconds=240
                if self.coverage
                and all(value == "complete" for value in self.coverage.values())
                else 3600,
            )


def finalize():
    """Finalize the latest staged sweep once, in the admission lock domain."""
    from factory.orchestration.factory_controls import (
        _audit,
        _locked_session,
        _read_session,
    )

    with _read_session() as db:
        pending = latest(db, "intake_pending")
        completed = latest(db, "intake_inventory")
        if pending is None or (completed is not None and completed.id > pending.id):
            return

    with _locked_session() as (db, _control):
        pending = latest(db, "intake_pending")
        completed = latest(db, "intake_inventory")
        if pending is None or (completed is not None and completed.id > pending.id):
            return
        data = json.loads(pending.detail_json)
        issues = data["issues"]
        identities = [(row["repo"], row["issue_number"]) for row in issues]
        items = (
            db.exec(
                select(WorkItem).where(
                    tuple_(WorkItem.github_repo, WorkItem.github_issue_number).in_(
                        identities
                    )
                )
            ).all()
            if identities
            else []
        )
        by_identity = {
            (item.github_repo, item.github_issue_number): item for item in items
        }
        ids = [item.id for item in items]
        blockers = (
            db.exec(
                select(WorkItemEdge.to_id, WorkItem.github_issue_number)
                .join(WorkItem, WorkItem.id == WorkItemEdge.from_id)
                .where(
                    WorkItemEdge.to_id.in_(ids),
                    WorkItemEdge.kind == "blocks",
                    WorkItem.state != "closed",
                )
            ).all()
            if ids
            else []
        )
        blocked = {}
        for target, number in blockers:
            if number is not None:
                blocked.setdefault(target, []).append(number)
        receipt_ids = [row.get("receipt_id") for row in issues if row.get("receipt_id")]
        receipts = (
            db.exec(
                select(FactoryReceipt).where(FactoryReceipt.id.in_(receipt_ids))
            ).all()
            if receipt_ids
            else []
        )
        by_receipt = {row.id: row for row in receipts}
        task_ids = [row.task_id for row in receipts if row.task_id]
        transitions = (
            db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.task_id.in_(task_ids),
                    FactoryAudit.action == "admit_next",
                )
            ).all()
            if task_ids
            else []
        )
        admitted_at = {
            json.loads(row.detail_json).get("receipt_id"): aware(
                row.created_at
            ).isoformat()
            for row in transitions
        }
        audit_rows = []
        for issue in issues:
            issue["sweep_id"] = data["sweep_id"]
            issue["observed_at"] = data["observed_at"]
            item = by_identity.get((issue["repo"], issue["issue_number"]))
            receipt = by_receipt.get(issue.get("receipt_id"))
            if receipt is not None and receipt.id in admitted_at:
                issue.update(
                    decision="admitted",
                    reason="admitted",
                    admitted_at=admitted_at[receipt.id],
                )
            elif issue["reason"] == "blocked" and item is not None:
                numbers = sorted(set(blocked.get(item.id, [])))
                issue["reason"] = (
                    ",".join(f"blocked_by:#{number}" for number in numbers)
                    or "blocked_by:unmapped"
                )
            elif issue["reason"] == "excluded_label":
                issue["reason"] = "excluded_label:" + ",".join(
                    issue.pop("excluded_labels", [])
                )
            issue.pop("labels", None)
            audit_rows.append(
                FactoryAudit(
                    actor="factory:intake",
                    action="intake_candidate",
                    detail_json=json.dumps(issue),
                )
            )
        db.add_all(audit_rows)
        # Carry forward each issue's latest observation when a later inventory
        # is partial or unavailable. Old timestamps remain old.
        previous = (
            json.loads(completed.detail_json).get("issues", []) if completed else []
        )
        merged = {(row["repo"], row["issue_number"]): row for row in previous}
        merged.update({(row["repo"], row["issue_number"]): row for row in issues})
        # Work-item state filters closed issues in the reader. Bound retained
        # observations to identities still present in the indexed inventory.
        open_keys = set(
            db.exec(
                select(WorkItem.github_repo, WorkItem.github_issue_number).where(
                    WorkItem.state != "closed"
                )
            ).all()
        )
        data["issues"] = [
            row for key, row in merged.items() if key in open_keys or key in identities
        ]
        _audit(db, "factory:intake", "intake_inventory", **data)


def backlog(db, now=None):
    """Latest snapshot plus current indexed work items, never full audit history."""
    now = aware(now or datetime.now(timezone.utc))
    snapshot = latest(db, "intake_inventory")
    swept = latest(db, "intake_swept")
    data = json.loads(snapshot.detail_json) if snapshot is not None else {}
    observations = {
        (row["repo"], row["issue_number"]): row for row in data.get("issues", [])
    }
    items = db.exec(
        select(WorkItem).where(
            WorkItem.state.in_(("open", "ready", "active", "deferred", "needs_human"))
        )
    ).all()
    keys = [(item.github_repo, item.github_issue_number) for item in items]
    newest_ids = (
        select(func.max(FactoryReceipt.id))
        .where(tuple_(FactoryReceipt.repo, FactoryReceipt.issue_number).in_(keys))
        .group_by(FactoryReceipt.repo, FactoryReceipt.issue_number)
    )
    receipts = (
        db.exec(select(FactoryReceipt).where(FactoryReceipt.id.in_(newest_ids))).all()
        if keys
        else []
    )
    admitted = {
        (row.repo, row.issue_number)
        for row in receipts
        if row.state in ("admitted", "landing", "uncertain", "succeeded")
    }
    rows = []
    observed = data.get("observed_at")
    stale = observed is None or now - aware(
        datetime.fromisoformat(observed)
    ) >= timedelta(seconds=data.get("fresh_for_seconds", 240))
    for item in items:
        if (
            "agent-ready" not in {label.lower() for label in item.labels}
            or item.github_issue_number is None
        ):
            continue
        prior = observations.get((item.github_repo, item.github_issue_number))
        created = item.github_created_at or (
            item.created_at if item.authority == "local" else None
        )
        prior_time = prior.get("observed_at") if prior else None
        row_stale = prior_time is not None and now - aware(
            datetime.fromisoformat(prior_time)
        ) >= timedelta(seconds=data.get("fresh_for_seconds", 240))
        rows.append(
            {
                "repo": item.github_repo,
                "issue_number": item.github_issue_number,
                "title": item.title,
                "created_at": aware(created).isoformat() if created else None,
                "age_days": max(0, (now - aware(created)).days) if created else None,
                "decision": prior.get("decision") if prior else None,
                "reason": prior.get("reason") if prior else "never_considered",
                "observed_at": prior.get("observed_at") if prior else None,
                "stale": row_stale,
                "subsequently_admitted": (item.github_repo, item.github_issue_number)
                in admitted,
            }
        )
    known = {(item.github_repo, item.github_issue_number) for item in items}
    # A listing can be observed even when synchronization or PR discovery fails.
    # Show those identities too, without claiming that their inventory is complete.
    missing = [key for key in observations if key not in known]
    closed = (
        set(
            db.exec(
                select(WorkItem.github_repo, WorkItem.github_issue_number).where(
                    tuple_(WorkItem.github_repo, WorkItem.github_issue_number).in_(
                        missing
                    ),
                    WorkItem.state.in_(("closed", "done")),
                )
            ).all()
        )
        if missing
        else set()
    )
    for key in missing:
        if key in closed:
            continue
        prior = observations[key]
        try:
            created = aware(
                datetime.fromisoformat(prior["created_at"].replace("Z", "+00:00"))
            )
        except (AttributeError, TypeError, ValueError):
            created = None
        rows.append(
            {
                **prior,
                "created_at": created.isoformat() if created else None,
                "age_days": max(0, (now - created).days) if created else None,
                "stale": stale,
                "subsequently_admitted": False,
            }
        )
    rows.sort(
        key=lambda row: (row["created_at"] or "9999", row["repo"], row["issue_number"])
    )
    return {
        "issues": rows,
        "coverage": data.get("coverage", {}),
        "stale": stale,
        "observed_at": observed,
        "available": snapshot is not None,
        "unfinished": swept is not None and swept.id != data.get("sweep_id"),
    }
