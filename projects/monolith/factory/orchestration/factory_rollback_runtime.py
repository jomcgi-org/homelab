"""Reconcile rollback effects without an alternate admission or landing lane.

Called by the synchronous landing tick (already running in a worker thread).
Network reads/writes happen outside sessions. Every database phase opens a
fresh session. Unknown issue-create outcomes are read back by marker and never
retried; a recovery receipt and its source branch have deterministic identities.
"""

from __future__ import annotations

import json

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_landing as landing
from factory.orchestration import factory_rollback as rollback
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt


def terminal_failure(task_id):
    with controls._read_session() as db:
        return (
            db.exec(
                select(FactoryAudit.id)
                .where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "rollout_failed",
                )
                .limit(1)
            ).first()
            is not None
        )


def observe_targets(policy, item, pull, targets):
    """Public landing caller: durable window and failure even while default-off."""
    if pull.get("merged") is not True or pull.get("merge_commit_sha") != item.get(
        "merge_commit_sha"
    ):
        return "rollback_observation_unknown"
    if type(pull.get("commits")) is not int or pull["commits"] < 1:
        return "rollback_observation_unknown"
    results = []
    for target in targets:
        delivery = {
            "repo": policy["repo"],
            "task_id": item["task_id"],
            "pr_number": item["pr_number"],
            "issue_number": item["issue_number"],
            "merge_commit_sha": pull["merge_commit_sha"],
            "merged_at": pull["merged_at"],
            "commit_count": pull["commits"],
            "source_checked": False,
            **{k: target[k] for k in ("application", "chart", "revision")},
        }
        observations = [
            {**delivery, **observation} for observation in target["observations"]
        ]
        judged = rollback.observe_window(
            policy, delivery, observations, now=landing._now()
        )
        results.append(judged["verdict"])
    if "rollout_failed" in results:
        return "rollout_failed"
    return (
        "rollout_verified"
        if results and all(r == "rollout_verified" for r in results)
        else "rollout_observing"
    )


def _rows(db, action):
    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == action)
        .order_by(FactoryAudit.id)
    ).all()


def source_details(delivery):
    from factory.orchestration.factory_reverts import is_revert_source

    repo, number = delivery["repo"], delivery["pr_number"]
    pull = landing.github_get(repo, f"pulls/{number}")
    if (
        pull.get("merged") is not True
        or pull.get("merge_commit_sha") != delivery["merge_commit_sha"]
    ):
        raise ValueError("source merge identity changed")
    commits = []
    for page in range(1, 11):
        batch = landing.github_list(
            repo, f"pulls/{number}/commits?per_page=100&page={page}"
        )
        commits.extend(batch)
        if len(batch) < 100:
            break
    if (
        type(pull.get("commits")) is not int
        or pull["commits"] < 1
        or len(commits) != pull["commits"]
    ):
        raise ValueError("source commit inventory incomplete")
    is_revert = is_revert_source(pull) or any(
        is_revert_source({"commit_message": c.get("commit", {}).get("message")})
        for c in commits
    )
    return {
        **delivery,
        "commit_count": pull["commits"],
        "source_checked": True,
        "is_revert": is_revert,
    }


def _details(db, action):
    return [json.loads(row.detail_json) for row in _rows(db, action)]


def admission_identity(db, row):
    """Validate the internal recovery link before choosing its source branch."""
    direction = json.loads(row.direction_json or "{}")
    key = direction.get("rollback_delivery_key")
    if key is None:
        return None
    bound = next(
        (
            d
            for d in _details(db, "rollback_receipt")
            if d.get("delivery_key") == key and d.get("receipt_id") == row.id
        ),
        None,
    )
    if bound is None or not any(
        d.get("delivery_key") == key for d in _details(db, "rollback_pr_started")
    ):
        raise ValueError("rollback recovery is not reserved")
    if parent_held(db, row):
        raise ValueError("rollback source ownership is held")
    return "rollback-" + key[:32]


def parent_held(db, row):
    direction = json.loads(getattr(row, "direction_json", None) or "{}")
    parent_id = direction.get("rollback_parent_task_id")
    if parent_id is None:
        return False
    parent = controls._receipt(db, parent_id)
    return (
        parent is None
        or parent.task_paused
        or parent.cancellation_requested
        or controls.landing_held(parent)
    )


def _record(delivery, action, token, **detail):
    with controls._locked_session() as (db, _control):
        if any(d.get("token") == token for d in _details(db, action)):
            return False
        rollback._append(
            db,
            delivery,
            action,
            rollback.identity(delivery),
            landing._now(),
            token=token,
            **detail,
        )
        return True


def _recorded(action, token):
    with controls._read_session() as db:
        return next((d for d in _details(db, action) if d.get("token") == token), None)


def _find_issue(repo, marker, since):
    # All states, no search-index lag. A truncated listing cannot authorize a
    # create. An unknown create is never retried even when the read is empty.
    matches = []
    for page in range(1, 11):
        batch = landing.github_list(
            repo,
            f"issues?state=all&sort=created&direction=desc&since={since}&per_page=100&page={page}",
        )
        matches.extend(
            i
            for i in batch
            if not i.get("pull_request")
            and marker in str(i.get("body") or "").splitlines()
        )
        if matches or len(batch) < 100:
            if len(matches) > 1:
                raise ValueError("duplicate rollback issue marker")
            return matches[0] if matches else None
    raise ValueError("rollback issue inventory incomplete")


def _issue_once(delivery, token, title, body, labels):
    previous = _recorded("rollback_external_issue_observed", token)
    if previous:
        return landing.github_get(
            delivery["repo"], f"issues/{previous['issue_number']}"
        )
    marker = f"<!-- factory-rollback:{token} -->"
    since = rollback.utc(delivery["merged_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = _find_issue(delivery["repo"], marker, since)
    if found is None:
        if not _record(delivery, "rollback_external_issue_started", token):
            return None
        found = landing.github_write(
            delivery["repo"],
            "issues",
            {
                "title": title,
                "body": marker + "\n\n" + body,
                "labels": labels,
            },
        )
    if not isinstance(found, dict) or type(found.get("number")) is not int:
        raise ValueError("rollback issue response unknown")
    _record(
        delivery,
        "rollback_external_issue_observed",
        token,
        issue_number=found["number"],
    )
    return found


def _parent_allows(delivery):
    issue = landing.github_get(delivery["repo"], f"issues/{delivery['issue_number']}")
    if any(
        str(label.get("name", "")).lower() in {"human", "needs-human"}
        for label in issue.get("labels", [])
    ):
        return False
    with controls._read_session() as db:
        row = controls._receipt(db, delivery["task_id"])
        return row is not None and not (
            row.task_paused or row.cancellation_requested or controls.landing_held(row)
        )


def recovery_prompt(delivery):
    encoded = json.dumps(delivery, separators=(",", ":"))
    return (
        f"Recover failed rollout of PR #{delivery['pr_number']} for issue #{delivery['issue_number']}.\n\n"
        "The factory reserved this source reversal. Implement only the full landed-range revert. "
        "In the assigned linked worktree, run the repository's "
        "projects/monolith/factory/orchestration/factory_rollback_source.py worker with --checkout, "
        "--delivery-json (the JSON below), and --recovery-issue (this issue number). "
        "The worker reverses newest-first, pushes the deterministic branch, and creates or adopts its PR. "
        "On retry, use the same branch and PR. Never create a second branch/PR or revert only the last commit. "
        "Repair source conflicts in that branch if necessary, retaining the whole rollback range. "
        "Use the ordinary required Linux CI, independent exact-head review, and normal merge queue. "
        "Do not change deployment pins, enable flags, release holds, or write to the cluster. "
        "The original issue is reopened only after rollback landing.\n\n"
        f"```json\n{encoded}\n```\n"
    )


def _receive(delivery, issue, policy):
    from factory.orchestration import factory_intake

    key = rollback.identity(delivery)
    with controls._locked_session() as (db, _control):
        original = controls._receipt(db, delivery["task_id"])
        if (
            original is None
            or original.task_paused
            or original.cancellation_requested
            or controls.landing_held(original)
        ):
            return
        result = factory_intake.receive_issue(
            delivery["repo"],
            issue["number"],
            issue["title"],
            issue["body"],
            f"https://github.com/{delivery['repo']}/issues/{issue['number']}",
            controls.INTAKE_ACTOR,
            generation=policy["generation"],
            issue=issue,
            session=db,
        )
        receipt_id = result["receipt"]["id"]
        row = db.get(FactoryReceipt, receipt_id)
        # This private direction is never derived from issue Markdown.
        direction = json.loads(row.direction_json or "{}")
        existing = direction.get("rollback_delivery_key")
        if existing is not None and existing != key:
            raise ValueError("recovery receipt is already owned")
        direction.update(
            rollback_delivery_key=key, rollback_parent_task_id=delivery["task_id"]
        )
        row.direction_json = json.dumps(direction)
        if not any(
            d.get("delivery_key") == key for d in _details(db, "rollback_receipt")
        ):
            rollback._append(
                db,
                delivery,
                "rollback_receipt",
                key,
                landing._now(),
                receipt_id=receipt_id,
            )
        return receipt_id


def _dispatch(delivery, policy):
    if not _parent_allows(delivery):
        return
    key = rollback.identity(delivery)
    issue = _issue_once(
        delivery,
        "recovery:" + key,
        f"revert({delivery['chart']}): recover PR #{delivery['pr_number']}",
        recovery_prompt(delivery),
        [],
    )
    if issue is None or issue.get("state") != "open":
        return
    if any(
        label.get("name") in {"human", "needs-human"}
        for label in issue.get("labels", [])
    ):
        return
    if _receive(delivery, issue, policy) is None:
        return
    # The durable creation fence identifies one logical recovery worker, not a
    # new attempt on each tick. After a crash the receipt/branch is reconciled.
    rollback.begin_pr_creation(delivery, now=landing._now())
    if not any(label.get("name") == "agent-ready" for label in issue.get("labels", [])):
        landing.github_write(
            delivery["repo"],
            f"issues/{issue['number']}/labels",
            {"labels": ["agent-ready"]},
        )


def _pull(delivery):
    key = rollback.identity(delivery)
    branch = "factory/rollback-" + key[:32]
    owner = delivery["repo"].split("/", 1)[0]
    found = landing.github_list(
        delivery["repo"],
        f"pulls?state=all&head={owner}:{branch}&base=main&per_page=100",
    )
    if len(found) > 1:
        raise ValueError("duplicate rollback pull requests")
    return (
        landing.github_get(delivery["repo"], f"pulls/{found[0]['number']}")
        if found
        else None
    )


def _owned_merge(delivery, pull):
    """The ordinary landing owner must attest its exact-head merge first."""
    key = rollback.identity(delivery)
    with controls._read_session() as db:
        link = next(
            (
                d
                for d in _details(db, "rollback_receipt")
                if d.get("delivery_key") == key
            ),
            None,
        )
        row = db.get(FactoryReceipt, link["receipt_id"]) if link else None
        if row is None or row.task_id is None:
            return False
        events = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "merged",
                FactoryAudit.actor == landing.ACTOR,
            )
        ).all()
        approved = landing._delivery_prs(db, [row.task_id]).get(row.task_id)
        return approved == (
            pull["number"],
            (pull.get("head") or {}).get("sha"),
        ) and any(
            (detail := json.loads(event.detail_json)).get("pr_number") == pull["number"]
            and detail.get("merge_commit_sha") == pull.get("merge_commit_sha")
            for event in events
        )


def _reopen(delivery, pull):
    key = rollback.identity(delivery)
    with controls._read_session() as db:
        landed = rollback._events(db, key, delivery["task_id"]).get(
            "rollback_landed", {}
        )
    if (
        pull.get("merged") is not True
        or landed.get("pr_number") != pull.get("number")
        or landed.get("merge_commit_sha") != pull.get("merge_commit_sha")
    ):
        raise ValueError("rollback has not landed")
    if _recorded("rollback_issue_reopened", key):
        return
    repo, number = delivery["repo"], delivery["issue_number"]
    issue = landing.github_get(repo, f"issues/{number}")
    # Update state and evidence in one idempotent PATCH. Re-read after a lost
    # response before patching; do not emit duplicate comment notifications.
    marker = f"<!-- rollback-landed:{key} -->"
    body = str(issue.get("body") or "")
    if marker not in body:
        failure = _recorded_failure(delivery)
        body += (
            f"\n\n{marker}\nRollout failed at {failure.get('observed_at', 'unknown')}: "
            f"{failure.get('signal', 'unknown')} on {delivery['application']} "
            f"revision {delivery['revision']}. Rollback PR #{pull['number']} "
            f"landed at {pull['merged_at']} ({pull['merge_commit_sha']}).\n"
        )
    if issue.get("state") != "open" or body != (issue.get("body") or ""):
        landing.github_write(
            repo, f"issues/{number}", {"state": "open", "body": body}, method="PATCH"
        )
    # Fresh ownership and receipt holds, after the reopen. Never replace the
    # label set or remove a person's labels.
    if _parent_allows(delivery):
        issue = landing.github_get(repo, f"issues/{number}")
        names = {label.get("name") for label in issue.get("labels", [])}
        if not names.intersection({"human", "needs-human", "agent-ready"}):
            landing.github_write(
                repo, f"issues/{number}/labels", {"labels": ["agent-ready"]}
            )
    _record(
        delivery,
        "rollback_issue_reopened",
        key,
        issue_number=number,
        pr_number=pull["number"],
    )
    settle(delivery)


def _recorded_failure(delivery):
    with controls._read_session() as db:
        return (
            rollback._events(db, rollback.identity(delivery), delivery["task_id"])
            .get("rollout_failed", {})
            .get("failure", {})
        )


def settle(delivery):
    """The existing outcome ledger says reverted; storage uses failed receipt.

    The original graph already finished before landing. Do not reset its plan,
    spend, holds, or starts. Keep a cancellation/human handoff outcome intact.
    """
    from factory.orchestration.models import SwarmTask

    key = rollback.identity(delivery)
    with controls._locked_session() as (db, _control):
        events = rollback._events(db, key, delivery["task_id"])
        if "rollback_landed" not in events or "rollback_issue_reopened" not in events:
            return False
        if any(d.get("delivery_key") == key for d in _details(db, "rollback_settled")):
            return True
        row = controls._receipt(db, delivery["task_id"])
        if row is None:
            return False
        if controls._quiescence_refusal(db, delivery["task_id"]):
            return False
        task = db.get(SwarmTask, delivery["task_id"])
        if row.state in {"landing", "succeeded"} and not row.cancellation_requested:
            row.state = "failed"
            task.settled_at = landing._now()
            row.updated_at = landing._now()
        if not db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == delivery["task_id"],
                FactoryAudit.action == "reverted",
            )
        ).first():
            rollback._append(
                db,
                delivery,
                "reverted",
                key,
                landing._now(),
                **{
                    k: v
                    for k, v in events["rollback_landed"].items()
                    if k not in {"delivery_key", "repo", "recorded_at"}
                },
            )
        rollback._append(
            db, delivery, "rollback_settled", key, landing._now(), outcome="reverted"
        )
        return True


def tick(policy):
    """Continue existing recovery work even when new rollback is disabled."""
    with controls._read_session() as db:
        failures = _details(db, "rollout_failed")
        intents = _details(db, "rollback_reserved")
        breakers = _details(db, "rollback_breaker_opened")
        settled = {d["delivery_key"] for d in _details(db, "rollback_settled")}
        issue_tokens = {
            d["token"] for d in _details(db, "rollback_external_issue_observed")
        }
    reserved = {d["delivery_key"] for d in intents}
    failures = [
        d
        for d in failures
        if d["repo"] == policy["repo"] and d["delivery_key"] not in reserved | settled
    ]
    intents = [
        d
        for d in intents
        if d["repo"] == policy["repo"] and d["delivery_key"] not in settled
    ]
    breakers = [
        d
        for d in breakers
        if d["repo"] == policy["repo"] and "breaker:" + d["opening"] not in issue_tokens
    ]
    for entry in failures[:20]:
        delivery = entry["delivery"]
        if delivery["repo"] != policy["repo"]:
            continue
        try:
            if not controls.rollback_policy(policy)["enabled"]:
                continue  # Failure is already durable; no new effects while off.
            delivery = source_details(delivery)
            decision = rollback.reserve(
                policy, delivery, entry["failure"], now=landing._now()
            )
            if decision["state"] == "needs_human":
                _issue_once(
                    delivery,
                    "revert-of-revert:" + rollback.identity(delivery),
                    "Factory rollback requires a person",
                    "Automatic revert of a revert refused.\n"
                    + recovery_prompt(delivery),
                    ["needs-human"],
                )
        except Exception as exc:  # noqa: BLE001 - isolate an unavailable source; preserve its ledger
            landing._error(delivery["task_id"], "rollback_reserve", exc)
    for entry in breakers[:20]:
        delivery = entry.get("delivery")
        if delivery is None or delivery["repo"] != policy["repo"]:
            continue
        try:
            _issue_once(
                delivery,
                "breaker:" + entry["opening"],
                "Factory rollback breaker opened",
                "Two automatic rollback reservations occurred in the rolling hour. "
                "Further automatic reversals are blocked.\n\n"
                + recovery_prompt(delivery),
                ["needs-human"],
            )
        except Exception as exc:  # noqa: BLE001 - an uncertain write is reconciled, never replayed
            landing._error(delivery["task_id"], "rollback_breaker_issue", exc)
    for entry in intents[:20]:
        delivery = entry["delivery"]
        if delivery["repo"] != policy["repo"]:
            continue
        try:
            pull = _pull(delivery)
            if pull is None:
                _dispatch(delivery, policy)
                continue
            if pull.get("merged") is True and not _owned_merge(delivery, pull):
                continue
            observed = rollback.observe_pr(delivery, pull, now=landing._now())
            if observed["state"] == "rollback_landed":
                _reopen(delivery, pull)
                settle(delivery)
            # An existing branch/PR is the same recovery receipt. The normal
            # conductor discovers its head and owns re-review and queueing.
        except Exception as exc:  # noqa: BLE001 - keep independent recovery receipts progressing
            landing._error(delivery["task_id"], "rollback_reconcile", exc)
