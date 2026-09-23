"""Deterministic, read-only factory and delivery baseline projection.

The module consumes previously exported lifecycle records. It never connects to
GitHub, the factory database, or a cluster. Missing coverage remains unavailable.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTRACT_VERSION = 1
EVENT_TYPES = {
    "task_intake",
    "agent_attempt_finished",
    "review_correction",
    "pr_opened",
    "pr_merged",
    "pr_closed_unmerged",
    "deployment_succeeded",
    "deployment_failed",
    "deployment_recovered",
    "verified_outcome",
    "outcome_reopened",
    "task_cancelled",
    "task_abandoned",
    "operator_intervention",
    "intervention_required",
}
COST_EVENT_TYPES = {"agent_attempt_finished", "review_correction"}
PR_EVENT_TYPES = {"pr_opened", "pr_merged", "pr_closed_unmerged"}
DEPLOYMENT_EVENT_TYPES = {
    "deployment_succeeded",
    "deployment_failed",
    "deployment_recovered",
}
TASK_EVENT_TYPES = EVENT_TYPES - PR_EVENT_TYPES - DEPLOYMENT_EVENT_TYPES


DEFINITIONS = {
    "repository_merge_count": {
        "start_event": None,
        "end_event": "pr_merged",
        "unit": "merged pull requests",
        "numerator": "distinct active pr_merged changes in the half-open window",
        "denominator": None,
        "source_query": "GitHub GraphQL command in factory/baseline.md",
        "inclusion": "pull requests whose merged_at is >= start and < end",
        "exclusion": "closed-unmerged pull requests, replayed records, and merges outside the window",
        "missing_data": "unavailable unless GitHub pagination covers the full window",
    },
    "repository_open_to_merge": {
        "start_event": "pr_opened",
        "end_event": "pr_merged",
        "unit": "seconds",
        "numerator": None,
        "denominator": "merged pull requests with both timestamps",
        "source_query": "GitHub GraphQL command in factory/baseline.md",
        "inclusion": "one elapsed interval per distinct merged pull request",
        "exclusion": "parallel attempt durations and changes missing either event",
        "missing_data": "reports known/total coverage and is unavailable with zero known intervals",
    },
    "factory_intake_to_verified_outcome": {
        "start_event": "task_intake",
        "end_event": "verified_outcome",
        "unit": "seconds",
        "numerator": None,
        "denominator": "accepted changes in the intake cohort",
        "source_query": "factory/baseline_export.sql",
        "inclusion": "tasks first received >= start and < end with active verified outcome evidence before end",
        "exclusion": "PR merge, deployment alone, retries as changes, and summed attempt duration",
        "missing_data": "right-censored tasks remain incomplete; zero accepted changes yields unavailable",
    },
    "factory_rework_rate": {
        "start_event": "review_correction",
        "end_event": "verified_outcome",
        "unit": "ratio",
        "numerator": "accepted cohort tasks with at least one review_correction",
        "denominator": "accepted changes",
        "source_query": "factory/baseline_export.sql",
        "inclusion": "failed review and correction work linked to the task",
        "exclusion": "replayed events and intervention-required signals",
        "missing_data": "unavailable with zero accepted changes or incomplete factory history",
    },
    "interventions_per_accepted_change": {
        "start_event": "operator_intervention",
        "end_event": "verified_outcome",
        "unit": "interventions per accepted change",
        "numerator": "explicit operator_intervention events with an actor",
        "denominator": "accepted changes",
        "source_query": "factory/baseline_export.sql",
        "inclusion": "completed operator-authored actions linked to cohort tasks",
        "exclusion": "intervention_required signals without a completed human action",
        "missing_data": "unavailable with zero accepted changes or incomplete audit coverage",
    },
    "cohort_cost_per_accepted_change": {
        "start_event": "task_intake",
        "end_event": "verified_outcome or other terminal task event",
        "unit": "USD per accepted change",
        "numerator": "cost of all expected cost events for every task in the intake cohort",
        "denominator": "accepted changes",
        "source_query": "factory/baseline_export.sql",
        "inclusion": "successful and failed attempts, review/correction work, cancelled and abandoned tasks",
        "exclusion": "reserved ceilings from known spend and events outside the intake cohort",
        "missing_data": "known subtotal is separate; complete value is unavailable if any expected cost is unknown",
    },
    "production_lead_time": {
        "start_event": "pr_merged",
        "end_event": "deployment_succeeded",
        "unit": "seconds",
        "numerator": None,
        "denominator": "production deployments with a linked merged change",
        "source_query": "live Argo, workload, digest, and chart receipt validation",
        "inclusion": "each managed application and production environment separately",
        "exclusion": "issue closure, PR merge without rollout, and verified outcome without deployment identity",
        "missing_data": "unavailable unless deployment coverage spans the full window",
    },
    "deployment_frequency": {
        "start_event": None,
        "end_event": "deployment_succeeded or deployment_failed",
        "unit": "deployments per UTC day",
        "numerator": "distinct production deployments in the half-open window",
        "denominator": "window duration in UTC days",
        "source_query": "live Argo, workload, digest, and chart receipt validation",
        "inclusion": "each managed application and production environment separately",
        "exclusion": "PR merges, retries of the same deployment identity, and issue closure",
        "missing_data": "unavailable unless deployment coverage spans the full window",
    },
    "change_failure_rate": {
        "start_event": "deployment_failed",
        "end_event": None,
        "unit": "ratio",
        "numerator": "distinct failed production deployments",
        "denominator": "distinct production deployments",
        "source_query": "live rollout and authoritative incident validation",
        "inclusion": "production failures attributed to a deployment",
        "exclusion": "failed agent attempts and CI failures without production impact",
        "missing_data": "unavailable unless deployment and incident failure coverage both span the window",
    },
    "failed_deployment_recovery_time": {
        "start_event": "deployment_failed",
        "end_event": "deployment_recovered",
        "unit": "seconds",
        "numerator": None,
        "denominator": "failed deployments with linked recovery evidence",
        "source_query": "live rollout and authoritative incident validation",
        "inclusion": "one elapsed interval per failed deployment",
        "exclusion": "issue closure and agent retry duration",
        "missing_data": "open failures are right-censored; incomplete incident coverage is unavailable",
    },
    "production_rework_rate": {
        "start_event": "deployment_failed",
        "end_event": "deployment_succeeded",
        "unit": "ratio",
        "numerator": "deployed changes requiring a later corrective deployment",
        "denominator": "distinct deployed changes",
        "source_query": "live rollout and authoritative incident validation",
        "inclusion": "successful corrective deployments whose corrects_deployment_id identifies the failed production deployment",
        "exclusion": "factory review corrections with no production failure",
        "missing_data": "unavailable unless deployment and incident coverage both span the window",
    },
}


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def completed_window(generated_at: datetime) -> tuple[datetime, datetime]:
    end = generated_at.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return end - timedelta(days=7), end


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _distribution(values: list[float], total: int, evidence: Iterable[str]) -> dict:
    status = (
        "observed"
        if values and len(values) == total
        else "partial"
        if values
        else "unavailable"
    )
    return {
        "status": status,
        "minimum": min(values) if values else None,
        "median": _median(values),
        "maximum": max(values) if values else None,
        "unit": "seconds",
        "known": len(values),
        "total": total,
        "coverage": len(values) / total if total else None,
        "reason": None if values else "no complete intervals",
        "evidence": sorted(set(evidence)),
    }


def _ratio(numerator: int, denominator: int, evidence: Iterable[str]) -> dict:
    return {
        "status": "observed" if denominator else "unavailable",
        "value": numerator / denominator if denominator else None,
        "unit": "ratio",
        "numerator": numerator,
        "denominator": denominator,
        "reason": None if denominator else "zero accepted denominator",
        "evidence": sorted(set(evidence)),
    }


def _event_key(event: dict) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def normalize_events(events: list[dict]) -> tuple[list[dict], dict]:
    selected: dict[str, dict] = {}
    replayed = older = revised = 0
    for raw in events:
        event = dict(raw)
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        revision = event.get("revision", 1)
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every event requires a stable event_id")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event_type for {event_id}: {event_type!r}")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError(f"revision for {event_id} must be a positive integer")
        required = []
        if event_type in TASK_EVENT_TYPES:
            required.append("task_id")
        if event_type in PR_EVENT_TYPES | DEPLOYMENT_EVENT_TYPES:
            required.append("change_id")
        if event_type in DEPLOYMENT_EVENT_TYPES:
            required.extend(("deployment_id", "application", "environment"))
        missing = [
            field
            for field in required
            if not isinstance(event.get(field), str) or not event[field]
        ]
        if missing:
            raise ValueError(f"{event_id} requires stable {', '.join(missing)}")
        event["revision"] = revision
        _timestamp(event.get("occurred_at"), f"occurred_at for {event_id}")
        for field in ("cost_usd", "reserved_cost_usd"):
            value = event.get(field)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise ValueError(f"{field} for {event_id} must be non-negative")
        corrects_deployment_id = event.get("corrects_deployment_id")
        if corrects_deployment_id is not None and (
            event_type != "deployment_succeeded"
            or not isinstance(corrects_deployment_id, str)
            or not corrects_deployment_id
        ):
            raise ValueError(
                f"corrects_deployment_id for {event_id} requires a deployment_succeeded event and a stable identity"
            )
        previous = selected.get(event_id)
        if previous is None:
            selected[event_id] = event
        elif revision == previous["revision"]:
            if _event_key(event) != _event_key(previous):
                raise ValueError(
                    f"conflicting replay for {event_id} revision {revision}"
                )
            replayed += 1
        elif revision > previous["revision"]:
            selected[event_id] = event
            revised += 1
        else:
            older += 1

    superseded = {
        event["supersedes_event_id"]
        for event in selected.values()
        if isinstance(event.get("supersedes_event_id"), str)
    }
    missing_targets = sorted(superseded - selected.keys())
    active = [event for key, event in selected.items() if key not in superseded]
    active.sort(
        key=lambda event: (
            _timestamp(event["occurred_at"], "occurred_at"),
            event["event_id"],
        )
    )
    return active, {
        "input_events": len(events),
        "unique_event_ids": len(selected),
        "active_events": len(active),
        "exact_replays_dropped": replayed,
        "older_revisions_dropped": older,
        "revised_events": revised,
        "superseded_events": len(superseded & selected.keys()),
        "missing_superseded_event_ids": missing_targets,
    }


def _covers(source: dict, start: datetime, end: datetime, **identity: str) -> bool:
    if not source.get("complete"):
        return False
    if any(source.get(key) != value for key, value in identity.items()):
        return False
    try:
        return (
            _timestamp(source["coverage_start"], "coverage_start") <= start
            and _timestamp(source["coverage_end"], "coverage_end") >= end
        )
    except (KeyError, TypeError, ValueError):
        return False


def _source_complete(
    sources: list[dict], kind: str, start: datetime, end: datetime, **identity: str
) -> bool:
    return any(
        source.get("kind") == kind and _covers(source, start, end, **identity)
        for source in sources
    )


def _evidence(events: Iterable[dict]) -> list[str]:
    return sorted(
        {
            event["evidence_url"]
            for event in events
            if isinstance(event.get("evidence_url"), str) and event["evidence_url"]
        }
    )


def _events_by(events: Iterable[dict], field: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for event in events:
        value = event.get(field)
        if isinstance(value, str) and value:
            result.setdefault(value, []).append(event)
    return result


def _distinct(events: Iterable[dict], field: str) -> list[dict]:
    """Collapse join fanout by a domain identity after event-id replay handling."""
    result: dict[str, dict] = {}
    for event in events:
        identity = event.get(field) or event["event_id"]
        previous = result.setdefault(identity, event)
        if previous is not event and any(
            previous.get(key) != event.get(key)
            for key in (
                "event_type",
                "occurred_at",
                "change_id",
                "deployment_id",
                "application",
                "environment",
            )
        ):
            raise ValueError(
                f"conflicting {field} evidence for {identity}; use a revision or supersedes_event_id"
            )
    return list(result.values())


def _production_application(
    application: dict,
    events: list[dict],
    sources: list[dict],
    start: datetime,
    end: datetime,
) -> dict:
    name = application.get("name")
    environment = application.get("environment")
    relevant = [
        event
        for event in events
        if event.get("application") == name
        and event.get("environment") == environment
        and start <= _timestamp(event["occurred_at"], "occurred_at") < end
    ]
    deployments = _distinct(
        (
            event
            for event in relevant
            if event["event_type"] in {"deployment_succeeded", "deployment_failed"}
        ),
        "deployment_id",
    )
    failures = [
        event for event in deployments if event["event_type"] == "deployment_failed"
    ]
    successes = [
        event for event in deployments if event["event_type"] == "deployment_succeeded"
    ]
    failures_by_id = {event["deployment_id"]: event for event in failures}
    recoveries = {
        event.get("deployment_id"): event
        for event in relevant
        if event["event_type"] == "deployment_recovered"
    }
    merged = {
        event.get("change_id"): event
        for event in events
        if event["event_type"] == "pr_merged"
    }
    deploy_complete = _source_complete(
        sources,
        "deployment",
        start,
        end,
        application=name,
        environment=environment,
    )
    incident_complete = _source_complete(
        sources,
        "incident",
        start,
        end,
        application=name,
        environment=environment,
    )
    lead_values = []
    for deployment in successes:
        prior = merged.get(deployment.get("change_id"))
        if prior is not None:
            seconds = (
                _timestamp(deployment["occurred_at"], "occurred_at")
                - _timestamp(prior["occurred_at"], "occurred_at")
            ).total_seconds()
            if seconds >= 0:
                lead_values.append(seconds)
    recovery_values = []
    for failure in failures:
        recovery = recoveries.get(failure.get("deployment_id"))
        if recovery is not None:
            seconds = (
                _timestamp(recovery["occurred_at"], "occurred_at")
                - _timestamp(failure["occurred_at"], "occurred_at")
            ).total_seconds()
            if seconds >= 0:
                recovery_values.append(seconds)
    deployed_changes = {
        event.get("change_id") for event in deployments if event.get("change_id")
    }
    reworked_changes = set()
    corrective_evidence = []
    for correction in successes:
        failure = failures_by_id.get(correction.get("corrects_deployment_id"))
        if failure is None:
            continue
        if _timestamp(correction["occurred_at"], "occurred_at") <= _timestamp(
            failure["occurred_at"], "occurred_at"
        ):
            continue
        if failure.get("change_id"):
            reworked_changes.add(failure["change_id"])
            corrective_evidence.extend((failure, correction))
    full_reliability = deploy_complete and incident_complete
    days = (end - start).total_seconds() / 86400
    unavailable = "deployment coverage does not span the window"
    reliability_unavailable = "deployment and incident coverage do not span the window"
    return {
        "name": name,
        "environment": environment,
        "service_boundary": application.get("service_boundary"),
        "inventory_source": application.get("inventory_source"),
        "inventory_status": application.get("inventory_status"),
        "deployment_frequency": {
            "status": "observed" if deploy_complete else "unavailable",
            "value": len(deployments) / days if deploy_complete else None,
            "unit": "deployments per UTC day",
            "numerator": len(deployments),
            "denominator": days,
            "reason": None if deploy_complete else unavailable,
            "evidence": _evidence(deployments),
        },
        "production_lead_time": (
            _distribution(lead_values, len(successes), _evidence(successes))
            if deploy_complete
            else {
                **_distribution([], len(successes), _evidence(successes)),
                "reason": unavailable,
            }
        ),
        "change_failure_rate": {
            "status": "observed" if full_reliability and deployments else "unavailable",
            "value": len(failures) / len(deployments)
            if full_reliability and deployments
            else None,
            "unit": "ratio",
            "numerator": len(failures),
            "denominator": len(deployments),
            "reason": None
            if full_reliability and deployments
            else reliability_unavailable
            if not full_reliability
            else "zero deployment denominator",
            "evidence": _evidence(failures),
        },
        "failed_deployment_recovery_time": (
            _distribution(recovery_values, len(failures), _evidence(relevant))
            if full_reliability
            else {
                **_distribution([], len(failures), _evidence(relevant)),
                "reason": reliability_unavailable,
            }
        ),
        "production_rework_rate": {
            "status": "observed"
            if full_reliability and deployed_changes
            else "unavailable",
            "value": len(reworked_changes) / len(deployed_changes)
            if full_reliability and deployed_changes
            else None,
            "unit": "ratio",
            "numerator": len(reworked_changes),
            "denominator": len(deployed_changes),
            "reason": None
            if full_reliability and deployed_changes
            else reliability_unavailable
            if not full_reliability
            else "zero deployed-change denominator",
            "evidence": _evidence(corrective_evidence),
        },
    }


def build_report(
    document: dict,
    *,
    generated_at: datetime,
    start: datetime,
    end: datetime,
) -> dict:
    if document.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")
    if not start < end:
        raise ValueError("window start must be before window end")
    sources = document.get("sources", [])
    applications = document.get("managed_applications", [])
    if not isinstance(sources, list) or not isinstance(applications, list):
        raise TypeError("sources and managed_applications must be lists")
    events, quality = normalize_events(document.get("events", []))
    before_end = [
        event
        for event in events
        if _timestamp(event["occurred_at"], "occurred_at") < end
    ]

    task_events = _events_by(before_end, "task_id")
    cohort: dict[str, list[dict]] = {}
    for task_id, linked in task_events.items():
        intakes = [event for event in linked if event["event_type"] == "task_intake"]
        if (
            intakes
            and start <= _timestamp(intakes[0]["occurred_at"], "occurred_at") < end
        ):
            cohort[task_id] = linked

    accepted_outcomes = {}
    for task_id, linked in cohort.items():
        transitions = [
            event
            for event in linked
            if event["event_type"] in {"verified_outcome", "outcome_reopened"}
        ]
        if transitions and transitions[-1]["event_type"] == "verified_outcome":
            accepted_outcomes[task_id] = transitions[-1]
    accepted = set(accepted_outcomes)
    cancelled = {
        task_id
        for task_id, linked in cohort.items()
        if any(event["event_type"] == "task_cancelled" for event in linked)
    }
    abandoned = {
        task_id
        for task_id, linked in cohort.items()
        if any(event["event_type"] == "task_abandoned" for event in linked)
    }
    incomplete = set(cohort) - accepted - cancelled - abandoned
    failed_attempts = [
        event
        for linked in cohort.values()
        for event in linked
        if event["event_type"] == "agent_attempt_finished"
        and event.get("outcome") == "failed"
    ]
    reworked = {
        task_id
        for task_id, linked in cohort.items()
        if task_id in accepted
        and any(event["event_type"] == "review_correction" for event in linked)
    }
    interventions = [
        event
        for linked in cohort.values()
        for event in linked
        if event["event_type"] == "operator_intervention" and event.get("actor")
    ]
    signals = [
        event
        for linked in cohort.values()
        for event in linked
        if event["event_type"] == "intervention_required"
    ]
    intake_durations = []
    accepted_evidence = []
    for task_id in accepted:
        linked = cohort[task_id]
        intake = min(
            (event for event in linked if event["event_type"] == "task_intake"),
            key=lambda event: _timestamp(event["occurred_at"], "occurred_at"),
        )
        verified = accepted_outcomes[task_id]
        duration = (
            _timestamp(verified["occurred_at"], "occurred_at")
            - _timestamp(intake["occurred_at"], "occurred_at")
        ).total_seconds()
        if duration >= 0:
            intake_durations.append(duration)
            accepted_evidence.extend(_evidence((intake, verified)))

    cost_events = [
        event
        for linked in cohort.values()
        for event in linked
        if event["event_type"] in COST_EVENT_TYPES or event.get("cost_expected") is True
    ]
    known_costs = [
        float(event["cost_usd"])
        for event in cost_events
        if event.get("cost_usd") is not None
    ]
    reserved = [
        float(event["reserved_cost_usd"])
        for event in cost_events
        if event.get("reserved_cost_usd") is not None
    ]
    known_subtotal = sum(known_costs)
    cost_complete = len(known_costs) == len(cost_events)
    accepted_count = len(accepted)

    merges = _distinct(
        (
            event
            for event in events
            if event["event_type"] == "pr_merged"
            and start <= _timestamp(event["occurred_at"], "occurred_at") < end
        ),
        "change_id",
    )
    opens = {
        event.get("change_id"): event
        for event in events
        if event["event_type"] == "pr_opened" and event.get("change_id")
    }
    merge_durations = []
    for merge in merges:
        opened = opens.get(merge.get("change_id"))
        if opened is not None:
            duration = (
                _timestamp(merge["occurred_at"], "occurred_at")
                - _timestamp(opened["occurred_at"], "occurred_at")
            ).total_seconds()
            if duration >= 0:
                merge_durations.append(duration)
    closed_unmerged = _distinct(
        (
            event
            for event in events
            if event["event_type"] == "pr_closed_unmerged"
            and start <= _timestamp(event["occurred_at"], "occurred_at") < end
        ),
        "change_id",
    )
    github_complete = _source_complete(sources, "github", start, end)

    factory_complete = _source_complete(sources, "factory", start, end)
    audit_complete = _source_complete(sources, "audit", start, end)
    cost_source_complete = _source_complete(sources, "cost", start, end)
    factory_reason = "factory history does not span the window"
    audit_reason = "operator audit coverage does not span the window"
    cost_reason = "cost source coverage does not span the window"
    factory_timing = _distribution(intake_durations, accepted_count, accepted_evidence)
    if not factory_complete:
        factory_timing.update(status="unavailable", reason=factory_reason)
    factory_rework = _ratio(len(reworked), accepted_count, _evidence(before_end))
    if not factory_complete:
        factory_rework.update(status="unavailable", value=None, reason=factory_reason)
    rework_inputs = [
        event
        for task_id in accepted
        for event in cohort[task_id]
        if event["event_type"] in COST_EVENT_TYPES
    ]
    if factory_complete and (
        not rework_inputs
        or any(event.get("rework_classified") is not True for event in rework_inputs)
    ):
        factory_rework.update(
            status="unavailable",
            value=None,
            reason="review/correction classification coverage is incomplete",
        )
    intervention_metric = _ratio(
        len(interventions), accepted_count, _evidence(interventions)
    )
    if not (factory_complete and audit_complete):
        intervention_metric.update(
            status="unavailable",
            value=None,
            reason=factory_reason if not factory_complete else audit_reason,
        )

    cost_status = (
        factory_complete
        and cost_source_complete
        and cost_complete
        and accepted_count > 0
    )
    cost_metric = {
        "status": "observed" if cost_status else "unavailable",
        "value": known_subtotal / accepted_count if cost_status else None,
        "unit": "USD per accepted change",
        "known_cost_subtotal_usd": known_subtotal if known_costs else None,
        "complete_cohort_cost_usd": (
            known_subtotal
            if factory_complete
            and cost_source_complete
            and cost_complete
            and cost_events
            else None
        ),
        "known_cost_items": len(known_costs),
        "total_expected_cost_items": len(cost_events),
        "coverage": len(known_costs) / len(cost_events) if cost_events else None,
        "accepted_denominator": accepted_count,
        "reserved_ceiling_subtotal_usd": sum(reserved) if reserved else None,
        "reason": None
        if cost_status
        else factory_reason
        if not factory_complete
        else cost_reason
        if not cost_source_complete
        else "one or more cohort costs are unknown"
        if not cost_complete
        else "zero accepted denominator",
        "evidence": _evidence(cost_events),
    }

    repository_count = {
        "status": "observed" if github_complete else "unavailable",
        "value": len(merges) if github_complete else None,
        "unit": "merged pull requests",
        "known": len(merges),
        "reason": None
        if github_complete
        else "GitHub coverage does not span the window",
        "evidence": _evidence(merges),
    }
    repository_timing = _distribution(merge_durations, len(merges), _evidence(merges))
    if not github_complete:
        repository_timing.update(
            status="unavailable", reason="GitHub coverage does not span the window"
        )

    production = [
        _production_application(application, events, sources, start, end)
        for application in applications
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _format_timestamp(generated_at),
        "source_as_of": document.get("source_as_of"),
        "window": {
            "start": _format_timestamp(start),
            "end": _format_timestamp(end),
            "semantics": "half-open UTC [start, end)",
        },
        "scope": {
            "repository": document.get("repository"),
            "factory_cohort": "tasks whose first task_intake is in the window",
            "accepted_change": "a cohort task with active verified_outcome evidence before window end",
            "production": "each listed managed application and environment is evaluated separately",
        },
        "definitions": DEFINITIONS,
        "source_coverage": sources,
        "data_quality": {
            **quality,
            "right_censored_tasks": len(incomplete) if factory_complete else None,
            "note": "Corrections replace their superseded event. Reopen evidence removes the superseded verified outcome from later regenerations.",
        },
        "counts": {
            "cohort_tasks": len(cohort) if factory_complete else None,
            "accepted_changes": accepted_count if factory_complete else None,
            "incomplete": len(incomplete) if factory_complete else None,
            "cancelled": len(cancelled) if factory_complete else None,
            "abandoned": len(abandoned) if factory_complete else None,
            "failed_agent_attempts": len(failed_attempts) if factory_complete else None,
            "intervention_required_signals": len(signals) if audit_complete else None,
            "explicit_operator_interventions": len(interventions)
            if audit_complete
            else None,
            "repository_closed_unmerged_attempts": (
                len(closed_unmerged) if github_complete else None
            ),
        },
        "metrics": {
            "repository_delivery": {
                "warning": "Repository merge is not production deployment or verified outcome.",
                "merge_count": repository_count,
                "pr_open_to_merge": repository_timing,
            },
            "factory": {
                "intake_to_verified_outcome": factory_timing,
                "rework_rate": factory_rework,
                "interventions_per_accepted_change": intervention_metric,
                "cohort_cost_per_accepted_change": cost_metric,
            },
            "managed_production_applications": production,
        },
        "slo_proposal": {
            "binding": False,
            "policy_change": False,
            "decision_owner": "Joe",
            "measurement_owner": "factory maintainers",
            "evaluation_window": "rolling 28 completed UTC days, after one fully covered window exists",
            "minimum_evidence": "non-zero denominators and complete factory, cost, deployment, incident, and operator-audit coverage for the evaluation window",
            "speed": {
                "target": None,
                "prerequisite": "complete merge-to-production and intake-to-verified intervals",
            },
            "reliability": {
                "target": None,
                "prerequisite": "complete deployment failure and recovery evidence",
            },
            "autonomy": {
                "target": None,
                "prerequisite": "validated operator-authored actions and actors",
            },
            "cost": {
                "target": None,
                "prerequisite": "complete cohort cost including failed, correction, and abandoned work",
            },
            "breach_response": "manually inspect evidence links and coverage, identify the measured bottleneck, and propose a reviewed corrective change; no automatic enforcement",
            "reason_targets_unset": "The repository-only baseline does not support numerical production, autonomy, or complete-cost targets.",
        },
    }


def _metric_rows(report: dict) -> Iterable[dict]:
    def row(scope: str, name: str, metric: dict) -> dict:
        return {
            "scope": scope,
            "metric": name,
            "status": metric.get("status"),
            "value": metric.get("value", metric.get("median")),
            "unit": metric.get("unit"),
            "numerator": metric.get("numerator"),
            "denominator": metric.get("denominator", metric.get("total")),
            "known": metric.get("known", metric.get("known_cost_items")),
            "total": metric.get("total", metric.get("total_expected_cost_items")),
            "reason": metric.get("reason"),
            "evidence": " ".join(metric.get("evidence", [])),
        }

    repository = report["metrics"]["repository_delivery"]
    yield row("repository", "merge_count", repository["merge_count"])
    yield row("repository", "pr_open_to_merge", repository["pr_open_to_merge"])
    for name, metric in report["metrics"]["factory"].items():
        yield row("factory", name, metric)
    for application in report["metrics"]["managed_production_applications"]:
        scope = f"{application['name']}:{application['environment']}"
        for name in (
            "production_lead_time",
            "deployment_frequency",
            "change_failure_rate",
            "failed_deployment_recovery_time",
            "production_rework_rate",
        ):
            yield row(scope, name, application[name])


def write_csv(report: dict, output) -> None:
    fields = [
        "scope",
        "metric",
        "status",
        "value",
        "unit",
        "numerator",
        "denominator",
        "known",
        "total",
        "reason",
        "evidence",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_metric_rows(report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="exported contract JSON")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="default: stdout")
    parser.add_argument("--generated-at", help="RFC3339 extraction time; default: now")
    parser.add_argument("--start", help="inclusive RFC3339 UTC override")
    parser.add_argument("--end", help="exclusive RFC3339 UTC override")
    args = parser.parse_args(argv)
    generated_at = (
        _timestamp(args.generated_at, "generated_at")
        if args.generated_at
        else datetime.now(timezone.utc)
    )
    default_start, default_end = completed_window(generated_at)
    start = _timestamp(args.start, "start") if args.start else default_start
    end = _timestamp(args.end, "end") if args.end else default_end
    document = json.loads(args.input.read_text())
    report = build_report(document, generated_at=generated_at, start=start, end=end)
    output = args.output.open("w", newline="") if args.output else sys.stdout
    try:
        if args.format == "csv":
            write_csv(report, output)
        else:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
