"""Bounded trusted inventory publication using the problem-issue adapter.

An intent may have crossed the write boundary even when no response survives.
Only a discovered exact output marker can reconcile that intent; absence never
authorizes another POST. Each finding has a cross-run deduplication key too.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from core.clock import aware
from sqlalchemy import JSON, cast, exists, type_coerce
from sqlalchemy.orm import aliased
from sqlmodel import select

from factory.orchestration import backlog_refill as refill
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_problem_issues as producer
from factory.orchestration.backlog_refill_policy import normalize
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt, WorkItem
from factory.orchestration.models import SwarmTask

TERMINAL = {"created", "existing", "rejected", "abandoned"}
SETTLED = TERMINAL | {"quarantined"}


def _last(db, output_id):
    row = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.actor == refill.ACTOR,
            FactoryAudit.action == refill.PREFIX + "output",
            FactoryAudit.detail_json.contains(f'"output_id":"{output_id}"'),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    return refill._detail(row) if row else None


def _key_state(db, marker):
    row = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.actor == refill.ACTOR,
            FactoryAudit.action == refill.PREFIX + "output",
            FactoryAudit.detail_json.contains('"dedup_marker":' + json.dumps(marker)),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    return refill._detail(row) if row else None


def _record(output, status, **extra):
    with controls._locked_session() as (db, _control):
        previous = _last(db, output["output_id"])
        if previous and previous["status"] in TERMINAL:
            return previous
        item = {**output, "status": status, **extra}
        refill.record(db, "output", **item)
        return item


def _allowed(db, control, output):
    """Recheck the owner at the committed write boundary, after discovery I/O."""
    policy = json.loads(control.policy_json or "{}") if control else {}
    if (
        control is None
        or control.state != "enabled"
        or policy.get("repo") != output["repo"]
        or not normalize(policy)["enabled"]
        or output["repo"] not in controls.enabled_repos(policy)
    ):
        return False
    if output["kind"] == "breaker":
        return True
    start = refill.latest_event(db, "started", run_id=output["run_id"])
    if start is None:
        return False
    detail = refill._detail(start)
    owner = db.get(FactoryReceipt, detail["receipt_id"])
    if (
        owner is None
        or owner.actor != refill.ACTOR
        or owner.repo != output["repo"]
        or owner.recurrence_key != output["run_id"]
        or owner.state != "admitted"
        or owner.task_paused
        or owner.cancellation_requested
        or owner.generation != policy.get("generation")
        or detail["policy_digest"] != refill.digest(policy)
        or refill.latest_event(db, "completed", run_id=output["run_id"])
    ):
        return False
    charter = db.get(WorkItem, owner.work_item_id) if owner.work_item_id else None
    if charter and "human" in {label.lower() for label in charter.labels}:
        return False
    task = db.get(SwarmTask, owner.task_id) if owner.task_id else None
    if task is None:
        return False
    pinned = json.loads(owner.policy_json or "{}")
    deadline = controls._recovery_deadline(db, owner.task_id) or (
        aware(task.created_at) + timedelta(seconds=pinned["task_timeout_seconds"])
    )
    return controls._now() < deadline


def reconcile_quarantined():
    """One due read per maintenance tick; never retry an ambiguous POST.

    Query latest output states in SQL so old audit bodies do not accumulate in
    process memory. Aliases carry a fence but only the original intent polls.
    """
    newer = aliased(FactoryAudit)
    with controls._read_session() as db:
        # SQLite JSON_EXTRACT accepts text directly; CAST(text AS JSON) would
        # coerce it to an integer. Postgres needs the explicit JSON cast.
        as_json = type_coerce if db.get_bind().dialect.name == "sqlite" else cast
        detail = as_json(FactoryAudit.detail_json, JSON)
        later = as_json(newer.detail_json, JSON)
        row = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.actor == refill.ACTOR,
                FactoryAudit.action == refill.PREFIX + "output",
                detail["status"].as_string() == "quarantined",
                detail["next_read"].as_string() <= controls._now().isoformat(),
                ~exists(
                    select(newer.id).where(
                        newer.actor == refill.ACTOR,
                        newer.action == refill.PREFIX + "output",
                        newer.id > FactoryAudit.id,
                        later["output_id"].as_string()
                        == detail["output_id"].as_string(),
                    )
                ),
            )
            .order_by(FactoryAudit.id)
            .limit(1)
        ).first()
        item = refill._detail(row) if row else None
    if item:
        publish(item, allow_new=False)


def publish(output, *, allow_new=True):
    """One output, at most one write, bounded read-only reconciliation."""
    output_id = output["output_id"]
    with controls._read_session() as db:
        last = _last(db, output_id)
        prior_key = _key_state(db, output["dedup_marker"])
    if last and last["status"] in TERMINAL:
        return last
    if not last and not allow_new:
        return _record(output, "abandoned")
    block = controls.problem_issues_policy({})
    now = controls._now()
    if last:
        if "next_read" not in last:
            return last  # Alias of a quarantined original intent.
        if now < aware(datetime.fromisoformat(last["next_read"])):
            return last
        # Reserve the read under the same lock before crossing the network.
        with controls._locked_session() as (db, _control):
            latest = _last(db, output_id)
            if latest != last:
                return latest
            reads = last.get("reads", 0) + 1
            exhausted = reads >= len(block["retry_minutes"])
            last = {
                **last,
                "status": "quarantined" if exhausted else "uncertain",
                "reads": reads,
                "next_read": (
                    now
                    + (
                        timedelta(hours=24)
                        if exhausted
                        else timedelta(minutes=block["retry_minutes"][reads])
                    )
                ).isoformat(),
            }
            refill.record(db, "output", **last)
        try:
            found, _cut = producer._discover(output["repo"], output["marker"], block)
        except Exception:  # noqa: BLE001 - failed reads retain the bounded fence
            return last
        if len(found) == 1:
            return _record(output, "created", issue_number=found[0], reconciled=True)
        return last

    if prior_key and prior_key["status"] in {"created", "existing"}:
        return _record(output, "existing", issue_number=prior_key["issue_number"])
    if prior_key and prior_key["status"] in {"uncertain", "quarantined"}:
        return _record(output, "quarantined", fenced_by=prior_key["output_id"])
    with controls._read_session() as db:
        control = db.get(controls.FactoryControl, "factory")
        if not _allowed(db, control, output):
            return None
    # Discover observed markers; the durable key ledger owns producer history.
    try:
        found, cut = producer._discover(output["repo"], output["dedup_marker"], block)
    except Exception:  # noqa: BLE001 - discovery errors authorize no writes
        return None
    if found:
        return _record(output, "existing", issue_number=found[0])
    # The committed key ledger covers every write from this producer. A capped
    # scan of unrelated history cannot authorize a replay of an existing intent.
    with controls._locked_session() as (db, control):
        if _last(db, output_id):
            return _last(db, output_id)
        if not _allowed(db, control, output):
            return None
        # A different run may have reserved this finding during discovery.
        prior_key = _key_state(db, output["dedup_marker"])
        if prior_key and prior_key["status"] in {
            "uncertain",
            "quarantined",
            "created",
            "existing",
        }:
            return None
        # This commit precedes POST, including the crash-before-send case.
        refill.record(
            db,
            "output",
            **output,
            status="uncertain",
            reads=0,
            scan_capped=cut,
            next_read=(now + timedelta(minutes=block["retry_minutes"][0])).isoformat(),
        )
    try:
        result = producer.github_write(
            output["repo"],
            {
                "title": output["title"],
                "body": output["body"],
                "labels": output["labels"],
            },
        )
        if (
            not isinstance(result, dict)
            or type(result.get("number")) is not int
            or result["number"] <= 0
            or output["marker"] not in (result.get("body") or "")
        ):
            return None
    except Exception as exc:  # noqa: BLE001 - uncertain writes must stay fenced
        if not producer._ambiguous(exc):
            return _record(output, "rejected", reason=type(exc).__name__)
        return None
    return _record(output, "created", issue_number=result["number"])


def collection(run_id, value):
    """Validate the bounded artifact before storing any publication authority."""
    if (
        not isinstance(value, dict)
        or set(value) != {"summary", "findings"}
        or not isinstance(value["summary"], str)
        or not 1 <= len(value["summary"]) <= 60000
        or not isinstance(value["findings"], list)
        or len(value["findings"]) > 100
    ):
        raise ValueError("invalid refill collection")
    keys = set()
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"key", "title", "body"}:
            raise ValueError("invalid refill finding")
        if (
            not isinstance(finding["key"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_./:-]{0,199}", finding["key"])
            or finding["key"] in keys
            or not isinstance(finding["title"], str)
            or not re.fullmatch(r"[a-z][a-z0-9/-]*: [^\n]{1,180}", finding["title"])
            or not isinstance(finding["body"], str)
            or not 1 <= len(finding["body"]) <= 12000
            or "<!-- factory-" in finding["body"]
        ):
            raise ValueError("invalid refill finding")
        keys.add(finding["key"])
    if (
        "\u2014" in json.dumps(value, ensure_ascii=False)
        or "<!-- factory-" in value["summary"]
    ):
        raise ValueError("invalid refill prose")
    with controls._locked_session() as (db, _control):
        start = refill.latest_event(db, "started", run_id=run_id)
        if start is None or len(value["findings"]) > refill._detail(start)[
            "policy"
        ].get("max_findings", 100):
            raise ValueError("refill collection exceeds pinned batch")
        prior = refill.latest_event(db, "collection", run_id=run_id)
        if prior is not None:
            if refill._detail(prior)["collection"] != value:
                raise ValueError("refill collection changed")
            return
        refill.record(db, "collection", run_id=run_id, collection=value)


def output(repo, run_id, kind, key, title, body, labels):
    output_id = refill.digest([repo, run_id, kind, key])
    marker = f"<!-- factory-refill-output:{output_id} -->"
    # Finding identity is server-derived; renaming the model's key cannot
    # turn the same finding into fresh yield. Summaries remain per-run.
    identity = " ".join(title.casefold().split()) if kind == "finding" else key
    dedup = f"<!-- factory-refill-finding:{refill.digest([repo, kind, identity])} -->"
    return {
        "repo": repo,
        "run_id": run_id,
        "kind": kind,
        "output_id": output_id,
        "marker": marker,
        "dedup_marker": dedup,
        "title": title,
        "body": f"{body}\n\n<!-- factory-refill:{run_id} -->\n{marker}\n{dedup}",
        "labels": labels,
    }


def advance(start, *, allow_new=True):
    """Publish one output per tick; yield counts only this run's new findings."""
    with controls._read_session() as db:
        row = refill.latest_event(db, "collection", run_id=start["run_id"])
    if row is None:
        return None
    value = refill._detail(row)["collection"]
    results = []
    for finding in value["findings"]:
        item = output(
            start["repo"],
            start["run_id"],
            "finding",
            finding["key"],
            finding["title"],
            finding["body"],
            ["agent-ready"],
        )
        with controls._read_session() as db:
            previous = _last(db, item["output_id"])
        result = (
            previous
            if previous and previous["status"] in SETTLED
            else publish(item, allow_new=allow_new)
        )
        if not result or result["status"] not in SETTLED:
            return None
        results.append(result)
        if previous is None:
            return None  # At most one new output boundary per tick.
    links = "\n".join(
        f"- #{item['issue_number']}" for item in results if "issue_number" in item
    )
    unresolved = "\n".join(
        f"- {item['title']}" for item in results if item["status"] == "quarantined"
    )
    publication_note = (
        "\n\nUnresolved publications (reconciliation continues without reposting):\n"
        + unresolved
        if unresolved
        else ""
    )
    summary = output(
        start["repo"],
        start["run_id"],
        "summary",
        start["run_id"],
        f"repo: project inventory {aware(row.created_at).date().isoformat()}",
        value["summary"] + "\n\nFinding issues:\n" + links + publication_note,
        ["needs-human"],
    )
    result = publish(summary, allow_new=allow_new)
    if not result or result["status"] not in SETTLED:
        return None
    return {
        "success": all(
            item["status"] in {"created", "existing"} for item in [*results, result]
        ),
        "inconclusive": any(
            item["status"] in {"quarantined", "abandoned"}
            for item in [*results, result]
        ),
        "new_issues": sum(item["status"] == "created" for item in results),
        "summary": value["summary"][:4000],
    }


def breaker(opening):
    runs = opening["runs"]
    run_id = runs[0]["run_id"] if runs else opening["opening_id"]
    return publish(
        output(
            opening["repo"],
            run_id,
            "breaker",
            opening["opening_id"],
            "factory: backlog refill breaker open",
            "Reason: "
            + opening["reason"]
            + "\n\n"
            + "\n\n".join(
                f"Run {item['run_id']}: {item.get('summary', 'durable start')}"
                for item in runs
            ),
            ["needs-human"],
        )
    )
