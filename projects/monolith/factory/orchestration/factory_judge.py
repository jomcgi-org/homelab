"""What the funding judge and the no-progress watchdog weigh, in one place.

Both decide whether a task should keep spending. With
``FACTORY_MERIT_JUDGE_ENABLED`` they decide on the merits a person would use:
how much verified progress exists, how close the work is to landing, and what
the issue is worth. ``CRITERIA`` is the prose both prompts carry, and
``merit_evidence`` is the bounded, server-read brief beside it. Tune the
judgement here, not in the callers; FACTORY.md documents the contract.

The hard ceilings are policy, not prose: see ``ceiling`` and the
``factoryMeritJudge*`` chart values. The judge chooses inside them and can
only ask a person when it doubts the work, never raise them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter

logger = logging.getLogger(__name__)

ENABLED_ENV = "FACTORY_MERIT_JUDGE_ENABLED"
MAX_MULTIPLE_ENV = "FACTORY_MERIT_JUDGE_MAX_EXTENSION_MULTIPLE"
MAX_USD_ENV = "FACTORY_MERIT_JUDGE_MAX_EXTENSION_USD"
DEFAULT_MAX_MULTIPLE = 1.0
DEFAULT_MAX_USD = 50.0

PRIORITY_ISSUES_ENV = "FACTORY_MERIT_JUDGE_PRIORITY_ISSUES"

# Labels that say an issue is worth more than a nice-to-have: real defects,
# high severity, and work tracked by a plan or decision record. Roadmap and
# factory-unblocking work (#6463, #6481) carries no label of its own in this
# repository, so it is named by PRIORITY_ISSUES_ENV and by a `factory:` title.
VALUE_LABELS = (
    "critical",
    "bug",
    "security-finding",
    "severity:high",
    "severity:medium",
    "plan-tracked",
    "adr-tracked",
    "roadmap",
)
LOW_VALUE_LABELS = ("severity:low", "good first issue", "stale")
FACTORY_TITLE = re.compile(r"^\s*factory\b", re.IGNORECASE)
_IMPLEMENTATION = re.compile(r"^(?:implement|correct|integrate)_")
_SHA = re.compile(r"^[0-9a-f]{40}$")
HISTORY_LIMIT = 8

CRITERIA = (
    "Decide on the merits, the way the operator would. Weigh three things. "
    "(1) Verified progress: commits pushed, an open pull request, green checks, "
    "review rounds passed, implementation nodes that succeeded. "
    "(2) How close it is to landing: a pull request that is clean or nearly "
    "green with only review or a correction left is close; no commit after "
    "several attempts is far. "
    "(3) Value: critical, high-severity, plan-tracked and real-bug issues, "
    "issues the operator named as priority, and factory work that unblocks the "
    "factory itself are worth more than a nice-to-have; an issue that blocks "
    "other open work is worth more. "
    "When progress is real, money already spent is a reason to finish, not to "
    "stop. Approve when the operator would likely approve: lots of verified "
    "progress, close to working, or very valuable. "
    "Escalate to the operator, rather than approving or stopping, only when "
    "you doubt it: little verified progress, repeated failure on the same "
    "step, or drift from the issue's scope. Stop only when the objective is no "
    "longer worth pursuing at all. Name the evidence behind your verdict."
)

ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["progress", "proximity", "value"],
    "properties": {
        "progress": {"type": "string", "minLength": 1, "maxLength": 1000},
        "proximity": {"type": "string", "minLength": 1, "maxLength": 1000},
        "value": {"type": "string", "minLength": 1, "maxLength": 1000},
        "doubts": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
}


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").lower() == "true"


def _amount(name: str, default: float) -> float:
    """A non-negative finite number from the environment, else the default."""
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def ceiling(pinned: dict) -> dict:
    """The most any judge grant may raise the receipt's pinned envelope.

    Measured from the policy the receipt pinned at admission, never from an
    earlier grant, so a run of grants cannot ratchet one task past a single
    total extension.
    """
    from factory.orchestration.factory_controls import task_turn_ceiling

    budget = float(pinned["task_budget_usd"])
    multiple = _amount(MAX_MULTIPLE_ENV, DEFAULT_MAX_MULTIPLE)
    cap = _amount(MAX_USD_ENV, DEFAULT_MAX_USD)
    extension = round(min(budget * multiple, cap), 6)
    turns = task_turn_ceiling(pinned)
    return {
        "pinned_task_budget_usd": budget,
        "max_extension_multiple": multiple,
        "max_extension_cap_usd": cap,
        "max_extension_usd": extension,
        "ceiling_usd": round(budget + extension, 6),
        "pinned_turns": turns,
        "turn_ceiling": turns + max(2, math.ceil(turns * multiple)),
    }


def needed(deficit: object, name: str) -> float | None:
    """The ``needed`` figure a structured envelope deficit records, or None."""
    entry = deficit.get(name) if isinstance(deficit, dict) else None
    value = entry.get("needed") if isinstance(entry, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def deficit_refusal(deficit: object, bound: dict) -> str | None:
    """Why a deficit is past what policy lets the judge fund, or None."""
    usd, turns = needed(deficit, "usd"), needed(deficit, "turns")
    if usd is None and turns is None:
        return "deficit_unreadable"
    if usd is not None and usd > bound["ceiling_usd"]:
        return "usd_over_ceiling"
    if turns is not None and turns > bound["turn_ceiling"]:
        return "turns_over_ceiling"
    return None


def _artifact(run: dict) -> dict:
    try:
        outcome = json.loads(run.get("outcome_json") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(outcome, dict):
        return {}
    value = outcome.get("value") or outcome.get("artifact") or {}
    return value if isinstance(value, dict) else {}


def progress(runs: list[dict]) -> dict:
    """Verified progress, from node runs alone (no GitHub read)."""
    statuses = Counter(run.get("status") for run in runs)
    implemented = [
        run
        for run in runs
        if _IMPLEMENTATION.match(str(run.get("node_key") or ""))
        and run.get("status") == "succeeded"
    ]
    heads = []
    for run in sorted(runs, key=lambda item: item.get("id") or 0):
        sha = _artifact(run).get("head_sha") or run.get("head_sha")
        if (
            isinstance(sha, str)
            and _SHA.fullmatch(sha)
            and sha not in heads
            and run.get("status") == "succeeded"
            and sha != run.get("base_sha")
        ):
            heads.append(sha)
    reviews = [
        run
        for run in runs
        if str(run.get("node_key") or "").startswith("review_")
        and run.get("status") == "succeeded"
    ]
    verdicts = [
        _artifact(run).get("verdict")
        for run in sorted(reviews, key=lambda item: item.get("id") or 0)
    ]
    failures = Counter(
        run.get("node_key") for run in runs if run.get("status") == "failed"
    )
    repeated = failures.most_common(1)[0] if failures else None
    return {
        "node_runs": dict(statuses),
        "implementation_succeeded": len(implemented),
        "commits_pushed": len(heads),
        "latest_head": heads[-1][:12] if heads else None,
        "review_verdicts": [v for v in verdicts if v][-HISTORY_LIMIT:],
        "reviews_approved": sum(1 for v in verdicts if v == "approve"),
        "most_repeated_failure": (
            {"node_key": repeated[0], "failures": repeated[1]} if repeated else None
        ),
    }


def proximity(task: dict, runs: list[dict]) -> dict:
    """How close the work is to landing: the pull request and its checks."""
    from factory.orchestration import factory_conductor as conductor

    number = conductor._latest_pr(runs) or task.get("delivery_pr_number")
    if not isinstance(number, int) or number <= 0:
        return {"pull_request": None}
    result: dict = {"pull_request": number}
    try:
        pull = conductor.github_get(task["repo"], f"pulls/{number}")
        head = (pull.get("head") or {}).get("sha")
        result.update(
            state=pull.get("state"),
            merged=bool(pull.get("merged")),
            draft=pull.get("draft"),
            mergeable_state=pull.get("mergeable_state"),
            head=head[:12] if isinstance(head, str) else None,
        )
        if isinstance(head, str) and _SHA.fullmatch(head):
            status = conductor.github_get(task["repo"], f"commits/{head}/status")
            result["checks"] = status.get("state")
    except Exception as exc:  # noqa: BLE001 - missing evidence is stated, not fatal
        result["unavailable"] = type(exc).__name__
    return result


def priority_issues() -> set[int]:
    """Issue numbers the operator has named as roadmap or unblocking work."""
    found = set()
    for part in os.getenv(PRIORITY_ISSUES_ENV, "").replace(",", " ").split():
        if part.lstrip("#").isdigit():
            found.add(int(part.lstrip("#")))
    return found


def value(task: dict) -> dict:
    """What the issue is worth: its labels, milestone and the work it blocks."""
    from factory.orchestration import factory_conductor as conductor

    result: dict = {
        "issue": task.get("issue_number"),
        "named_priority": task.get("issue_number") in priority_issues(),
    }
    try:
        issue = conductor.github_get(task["repo"], f"issues/{task['issue_number']}")
        labels = sorted(
            str(label.get("name") if isinstance(label, dict) else label).lower()
            for label in issue.get("labels") or []
        )
        milestone = issue.get("milestone")
        title = str(issue.get("title") or "")
        result.update(
            title=title[:200],
            factory_work=bool(FACTORY_TITLE.match(title)),
            labels=labels,
            value_labels=[name for name in labels if name in VALUE_LABELS],
            low_value_labels=[name for name in labels if name in LOW_VALUE_LABELS],
            milestone=milestone.get("title") if isinstance(milestone, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001 - missing evidence is stated, not fatal
        result["unavailable"] = type(exc).__name__
    try:
        result["blocks_open_work"] = _blocks_open(task)
    except Exception:
        logger.debug("factory judge could not count blocked work", exc_info=True)
    return result


def _blocks_open(task: dict) -> int:
    from sqlmodel import select

    from factory.orchestration.factory_controls import _read_session
    from factory.orchestration.factory_models import WorkItem, WorkItemEdge

    with _read_session() as db:
        source = db.exec(
            select(WorkItem.id).where(
                WorkItem.github_repo == task["repo"],
                WorkItem.github_issue_number == task["issue_number"],
            )
        ).first()
        if source is None:
            return 0
        targets = db.exec(
            select(WorkItemEdge.to_id).where(
                WorkItemEdge.kind == "blocks", WorkItemEdge.from_id == source
            )
        ).all()
        if not targets:
            return 0
        return len(
            db.exec(
                select(WorkItem.id).where(
                    WorkItem.id.in_(list(targets)), WorkItem.state != "closed"
                )
            ).all()
        )


def merit_evidence(task: dict, runs: list[dict]) -> dict:
    """The bounded brief: at most three GitHub reads, never raises."""
    return {
        "progress": progress(runs),
        "proximity": proximity(task, runs),
        "value": value(task),
    }
