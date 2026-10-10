"""Operator-only, durable and serialized submission of fixed KG review jobs.

Kubernetes failures are uncertain outcomes. A reserved receipt is never retried
or released without terminal Workflow evidence, even after Workflow GC.
"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

from cluster.api import KubernetesClient
from core.db import get_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from knowledge.models import ReviewPilotRun

NAMESPACE = "monolith-workflows"
MUTEX = {"mutexes": [{"name": "knowledge-review-pilot"}]}
BACKFILL_ARGS = [
    "knowledge-review-backfill",
    "--pending-only",
    "--batch-size",
    "20",
    "--max-batches",
    "1",
]
ADMISSION_ARGS = [
    "knowledge-review-admission",
    "--batch-size",
    "20",
    "--max-requests",
    "60",
    "--deadline-seconds",
    "240",
]
JOBS = {
    "knowledge-review-backfill-dry-run": (BACKFILL_ARGS, 240),
    "knowledge-review-backfill-pilot": (
        [BACKFILL_ARGS[0], "--apply", *BACKFILL_ARGS[1:]],
        240,
    ),
    "knowledge-review-admission-dry-run": (ADMISSION_ARGS, 240),
    "knowledge-review-admission": (
        [ADMISSION_ARGS[0], "--apply", *ADMISSION_ARGS[1:]],
        300,
    ),
}
GATES = {
    "knowledge-review-backfill-pilot": "knowledge-review-backfill-dry-run",
    "knowledge-review-admission": "knowledge-review-admission-dry-run",
}
CONTROL_JOBS = {*JOBS, "knowledge-review-backfill"}
TERMINAL = {"Succeeded", "Failed", "Error"}
REPORT_FIELDS = {
    "dry_run",
    "count",
    "policies",
    "freshness",
    "next_after",
    "candidates",
    "blocked",
    "renewed",
    "aborted",
    "failed",
    "unsupported",
    "unavailable",
    "errors",
    "budget_exhausted",
    "deadline_reached",
    "requests",
    "note_ids",
    "outcomes",
}


def request_key(value: str) -> str:
    key = str(UUID(value))
    if key != value:
        raise ValueError("request_id must be a canonical UUID")
    return key


def _view(row: ReviewPilotRun) -> dict:
    return {
        "request_id": row.request_id,
        "job": row.job,
        "actor": row.actor,
        "workflow_name": row.workflow_name,
        "namespace": NAMESPACE,
        "created_at": row.created_at.isoformat(),
        "dry_run_request_id": row.dry_run_request_id,
        "active": row.active_slot is not None,
        "result": row.result,
    }


def _read(request_id: str | None = None) -> dict | None:
    with Session(get_engine()) as session:
        row = (
            session.get(ReviewPilotRun, request_id)
            if request_id
            else session.exec(
                select(ReviewPilotRun).where(ReviewPilotRun.active_slot == 1)
            ).first()
        )
        return _view(row) if row else None


def _reserve(
    job: str, request_id: str, actor: str, dry_run_request_id: str | None
) -> dict:
    with Session(get_engine()) as session:
        existing = session.get(ReviewPilotRun, request_id)
        if existing:
            if existing.job != job or existing.dry_run_request_id != dry_run_request_id:
                raise ValueError(
                    "request_id already belongs to different submission arguments"
                )
            return {**_view(existing), "created": False}
        if session.exec(
            select(ReviewPilotRun).where(ReviewPilotRun.active_slot == 1)
        ).first():
            raise ValueError(
                "a review submission is active or uncertain; inspect its receipt first"
            )
        if job in GATES:
            gate = (
                session.get(ReviewPilotRun, dry_run_request_id)
                if dry_run_request_id
                else None
            )
            if not gate or gate.job != GATES[job] or gate.active_slot is not None:
                raise ValueError(
                    "application requires the matching completed dry-run receipt"
                )
            report = gate.result.get("report", {})
            observed = (
                gate.created_at.replace(tzinfo=timezone.utc)
                if gate.created_at.tzinfo is None
                else gate.created_at
            )
            if (
                gate.result.get("phase") != "Succeeded"
                or report.get("dry_run") is not True
                or not 0 < report.get("count", report.get("candidates", 0)) <= 20
                or datetime.now(timezone.utc) - observed > timedelta(minutes=15)
            ):
                raise ValueError(
                    "dry-run receipt must be successful, nonempty and at most 15 minutes old"
                )
        elif dry_run_request_id is not None:
            raise ValueError("dry-run jobs do not accept a dry_run_request_id")
        row = ReviewPilotRun(
            request_id=request_id,
            active_slot=1,
            job=job,
            actor=actor,
            workflow_name=f"kg-review-{request_id}",
            dry_run_request_id=dry_run_request_id,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(
                "concurrent or duplicate submission; inspect the existing receipt"
            ) from exc
        session.refresh(row)
        return {**_view(row), "created": True}


def _record(request_id: str, result: dict) -> dict:
    with Session(get_engine()) as session:
        row = session.exec(
            select(ReviewPilotRun)
            .where(ReviewPilotRun.request_id == request_id)
            .with_for_update()
        ).one()
        # Late observations must not overwrite an already recorded terminal result.
        if row.active_slot is not None:
            row.result = result
            if result.get("phase") in TERMINAL:
                row.active_slot = None
            session.commit()
        return _view(row)


def controls(items: list[dict]) -> tuple[dict, dict]:
    by_name = {item.get("metadata", {}).get("name"): item for item in items}
    summaries = {}
    for name in sorted(CONTROL_JOBS):
        item = by_name.get(name, {})
        spec = item.get("spec", {})
        workflow = spec.get("workflowSpec", {})
        summaries[name] = {
            "present": bool(item),
            "suspended": spec.get("suspend"),
            "schedules": spec.get("schedules", []),
            "concurrency_policy": spec.get("concurrencyPolicy"),
            "deadline_seconds": workflow.get("activeDeadlineSeconds"),
            "serialized": workflow.get("synchronization") == MUTEX,
            "last_scheduled_at": item.get("status", {}).get("lastScheduledTime"),
        }
    return summaries, by_name


def checked_spec(job: str, items: list[dict]) -> dict:
    summaries, by_name = controls(items)
    if any(
        not v["present"] or v["suspended"] is not True or not v["serialized"]
        for v in summaries.values()
    ):
        raise ValueError(
            "all review controls must be present, suspended and share the review mutex"
        )
    cron = by_name[job]
    if cron.get("metadata", {}).get("namespace") != NAMESPACE:
        raise ValueError("unexpected workflow namespace")
    spec = deepcopy(cron["spec"]["workflowSpec"])
    expected_args, deadline = JOBS[job]
    templates = spec.get("templates", [])
    if (
        cron["spec"].get("concurrencyPolicy") != "Forbid"
        or spec.get("entrypoint") != "run"
        or len(templates) != 1
        or templates[0].get("name") != "run"
        or templates[0].get("container", {}).get("args") != expected_args
        or spec.get("activeDeadlineSeconds") != deadline
        or spec.get("arguments")
        or spec.get("onExit")
        or spec.get("hooks")
        or spec.get("retryStrategy")
        or templates[0].get("retryStrategy")
        or templates[0].get("outputs")
        != {
            "parameters": [
                {
                    "name": "review-report",
                    "valueFrom": {"path": "/tmp/knowledge-review-report.json"},
                }
            ]
        }
    ):
        raise ValueError(
            "deployed review template differs from the fixed pilot contract"
        )
    # The chart owns the image, credentials and execution spec. Caller input
    # never becomes a manifest, container argument, namespace or template name.
    return spec


def workflow_result(workflow: dict, receipt: dict) -> dict:
    meta = workflow.get("metadata", {})
    labels = meta.get("labels", {})
    if (
        meta.get("name") != receipt["workflow_name"]
        or meta.get("namespace") != NAMESPACE
        or labels.get("monolith.jomcgi.dev/review-request") != receipt["request_id"]
        or labels.get("monolith.jomcgi.dev/review-job") != receipt["job"]
    ):
        raise ValueError("workflow identity does not match the audited request")
    status = workflow.get("status", {})
    result = {k: status.get(k) for k in ("phase", "startedAt", "finishedAt")}
    # Only the named JSON output is returned. Never expose node messages,
    # environment, parameters from other jobs, pod manifests or raw logs.
    for node in (status.get("nodes") or {}).values():
        if node.get("templateName") != "run":
            continue
        for param in node.get("outputs", {}).get("parameters", []):
            if param.get("name") == "review-report":
                raw = param.get("value", "")
                if len(raw) > 32_768:
                    raise ValueError("review report exceeds the receipt limit")
                report = json.loads(raw)
                if not isinstance(report, dict) or set(report) - REPORT_FIELDS:
                    raise ValueError("unexpected review report fields")
                result["report"] = report
    return result


async def inspect(request_id: str | None = None) -> dict:
    if request_id is not None:
        request_key(request_id)
    client = KubernetesClient()
    try:
        summary, _ = controls(await client.list_cronworkflows(NAMESPACE))
        receipt = await asyncio.to_thread(_read, request_id)
        if receipt and receipt["active"]:
            workflow = await client.get_workflow(NAMESPACE, receipt["workflow_name"])
            if workflow:
                receipt = await asyncio.to_thread(
                    _record, receipt["request_id"], workflow_result(workflow, receipt)
                )
            else:
                receipt = {
                    **receipt,
                    "warning": "workflow absent; submission remains fenced",
                }
        return {"controls": summary, "receipt": receipt}
    finally:
        await client.close()


async def submit(
    job: str, request_id: str, actor: str, dry_run_request_id: str | None = None
) -> dict:
    if job not in JOBS:
        raise ValueError("unsupported review job")
    request_key(request_id)
    if dry_run_request_id is not None:
        request_key(dry_run_request_id)
    if os.environ.get("SCHEDULER_WORKFLOW_NAMESPACE") != NAMESPACE:
        raise ValueError(
            "pilot submission is available only on the production monolith"
        )
    existing = await asyncio.to_thread(_read, request_id)
    if existing:
        if (
            existing["job"] != job
            or existing["dry_run_request_id"] != dry_run_request_id
        ):
            raise ValueError(
                "request_id already belongs to different submission arguments"
            )
        return existing
    client = KubernetesClient()
    try:
        spec = checked_spec(job, await client.list_cronworkflows(NAMESPACE))
        # Reconcile out-of-band Argo runs too. A manual submission can race this
        # read, so the controller mutex remains the final execution fence.
        workflows = await client.list_workflows(NAMESPACE)
        if any(
            w.get("status", {}).get("phase") not in TERMINAL
            and (
                w.get("metadata", {})
                .get("labels", {})
                .get("workflows.argoproj.io/cron-workflow")
                in CONTROL_JOBS
                or w.get("spec", {}).get("synchronization") == MUTEX
            )
            for w in workflows
        ):
            raise ValueError("a review workflow is already pending or running")
        receipt = await asyncio.to_thread(
            _reserve, job, request_id, actor, dry_run_request_id
        )
        if not receipt.pop("created"):
            return receipt
        manifest = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "name": receipt["workflow_name"],
                "namespace": NAMESPACE,
                "labels": {
                    "monolith.jomcgi.dev/review-request": request_id,
                    "monolith.jomcgi.dev/review-job": job,
                    "workflows.argoproj.io/cron-workflow": job,
                },
            },
            "spec": spec,
        }
        try:
            await client.create_workflow(NAMESPACE, manifest)
        except Exception:  # noqa: BLE001 - every uncertain create must retain its fence
            # Persist uncertainty before returning. Never replay create: even a
            # missing object could have completed and been garbage-collected.
            return await asyncio.to_thread(
                _record,
                request_id,
                {
                    "phase": "Unknown",
                    "reason": "submission outcome uncertain; inspect this request",
                },
            )
        return await asyncio.to_thread(_record, request_id, {"phase": "Submitted"})
    finally:
        await client.close()
