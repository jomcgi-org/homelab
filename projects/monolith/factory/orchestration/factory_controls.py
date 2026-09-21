"""Durable operator authority and conservative factory start reservations.

Every mutation locks the singleton control row before inspecting receipts. The
write lock also serializes file-backed SQLite tests; admission never relies on
an unlocked count. Issue text and conductor artifacts cannot configure policy.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import re
from typing import Iterator

from sqlalchemy import or_, update
from sqlmodel import Session, select

from core.db import get_engine
from factory.orchestration.factory_models import (
    DEFAULT_TASK_CLASS,
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    MAX_CAPACITY_DENIED_ATTEMPTS,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

_ACTIVE_START_SESSION: ContextVar[Session | None] = ContextVar(
    "factory_active_start_session", default=None
)

MAX_LANDING_RECOVERIES = 2
LANDING_RECOVERY_TIMEOUT_SECONDS = 3600

_ACTIVE = ("admitted", "uncertain")
_TERMINAL = ("succeeded", "failed", "cancelled")
# A receipt whose task asked a person for a decision and left the lane to wait
# for it. It holds no slot, starts nothing and keeps its graph, and it leaves
# this state only when an operator decides: a re-admission returns it to
# queued, every other answer settles it cancelled.
ESCALATED = "escalated"
# Every state a reconciler treats as finished. An escalated receipt is not
# terminal, because a decision can still return it to the lane, but nothing
# the server does on its own will move it either.
_SETTLED = (*_TERMINAL, ESCALATED)
GENERATION_RECONCILIATION_LIMIT = 50
GENERATION_RETIREMENT_ACTOR = "factory:generation-retirement"
_DELIVERY_BRANCH = re.compile(
    r"factory/[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9_-])?"
)
_POLICY_KEYS = {
    "repo",
    "issue_numbers",
    "generation",
    "max_tasks",
    "max_turns_per_task",
    "max_task_turns_hard",
    "max_parallel_nodes",
    "max_planner_turns",
    "task_budget_usd",
    "turn_budget_usd",
    "allowed_models",
    "conductor_model",
    "reviewer_model",
    "base_branch",
    "turn_timeout_seconds",
    "max_attempts",
    "worker_model",
    "task_timeout_seconds",
    "model_pools",
    "max_review_rounds",
    "max_review_recovery_rounds",
    "intake",
    "problem_issues",
    "quota_guard",
    "auto_merge",
}
_OPTIONAL_POLICY_KEYS = {
    "reviewer_model",
    "model_pools",
    "max_planner_turns",
    "max_review_rounds",
    "max_review_recovery_rounds",
    "max_task_turns_hard",
    "max_turns_per_task",
    "max_parallel_nodes",
    "intake",
    "problem_issues",
    "quota_guard",
    "auto_merge",
}


def validate_delivery_branch(branch: object) -> str:
    """Return an operator-granted factory branch or reject it explicitly."""
    if branch == "main":
        raise ValueError("delivery branch main is forbidden")
    if not isinstance(branch, str) or _DELIVERY_BRANCH.fullmatch(branch) is None:
        raise ValueError("delivery branch must be in the factory/ namespace")
    if ".." in branch or "//" in branch or "/." in branch or branch.endswith(".lock"):
        raise ValueError("delivery branch must be a valid factory/ branch")
    return branch


def validate_pr_branch(branch: object) -> str:
    """A GitHub-verified adopted head may be outside the factory namespace."""
    if not isinstance(branch, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", branch
    ):
        raise ValueError("invalid adopted PR branch")
    if (
        branch in ("main", "master")
        or any(
            part in ("", ".", "..") or part.startswith(".") or part.endswith(".lock")
            for part in branch.split("/")
        )
        or ".." in branch
        or branch.endswith(".")
    ):
        raise ValueError("unsafe adopted PR branch")
    return branch


def granted_delivery_surface(direction: object) -> tuple[str | None, int | None]:
    """The branch and PR an operator direction grants, if it grants either."""
    if not isinstance(direction, dict):
        return None, None
    number = direction.get("delivery_pr_number", direction.get("prior_pr_number"))
    if number is None:
        return None, None
    if type(number) is not int or number <= 0:
        raise ValueError("the prior pull request number is invalid")
    branch = direction.get("delivery_branch", direction.get("prior_branch"))
    return (
        validate_pr_branch(branch)
        if direction.get("delivery_adoption")
        else validate_delivery_branch(branch)
    ), number


def delivery_branch_owner(
    db: Session,
    repo: str,
    branch: str,
    *,
    exclude_receipt_id: int | None = None,
) -> str | None:
    """The active task that owns ``branch`` in ``repo``, if there is one."""
    validate_pr_branch(branch)
    rows = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.repo == repo,
            FactoryReceipt.state.in_(_ACTIVE),
        )
    ).all()
    for row in rows:
        if row.id == exclude_receipt_id or not row.task_id:
            continue
        direction = json.loads(row.direction_json) if row.direction_json else {}
        granted, _number = granted_delivery_surface(direction)
        owned = granted or f"factory/{row.task_id}"
        if owned == branch or branch.startswith(f"factory/{row.task_id}-"):
            # Parallel source-writing nodes retain their running task's ownership.
            return row.task_id
    return None


# Bounded review, correct and re-review rounds the engine runs on its own before
# it asks the planner. Absent from a live policy means this default, so the
# server gains the bound without an operator re-post.
DEFAULT_MAX_REVIEW_ROUNDS = 2
# Extra rounds require explicit policy and fresh passing CI. They use the same
# task envelope and never re-admit a receipt or reset its accounting.
DEFAULT_MAX_REVIEW_RECOVERY_ROUNDS = 0
# Autonomous intake is off until an operator turns it on. A policy that
# predates the block reads these defaults, so the lane gains the shape
# without gaining the behaviour.
DEFAULT_INTAKE = {
    "enabled": False,
    "labels": ["agent-ready"],
    "exclude_labels": ["needs-human", "wontfix", "security-finding"],
    "max_per_day": 5,
    "cooldown_hours": 24,
    "refine_enabled": False,
    # Closing an issue is the one refine outcome that destroys something an
    # operator would have to undo by hand, so it is a flag of its own and the
    # rest of the refine path works without it.
    "close_enabled": False,
    "max_closes_per_day": 3,
}
# Landing is off until an operator turns it on. Arming a merge is the first
# thing the reconciler does that changes the repository rather than reading
# it, so the capability arrives inert and an operator opts in.
DEFAULT_AUTO_MERGE = False
# Problem issues turn a deliberately small set of exact factory audits into
# ordinary GitHub issues. Both the producer and every source arrive off. The
# remaining values are conservative repository defaults supplied by the
# conductor rescope for #6002; changing live policy remains an operator act.
PROBLEM_ISSUE_SOURCES = (
    "node_stalled",
    "workflow_stranded",
    "landing_recovery_exhausted",
)
DEFAULT_PROBLEM_ISSUES = {
    "enabled": False,
    "sources": {source: False for source in PROBLEM_ISSUE_SOURCES},
    "source_audit_limit": 50,
    "issue_pages": 2,
    "issues_per_page": 100,
    "max_per_tick": 1,
    "max_per_24_hours": 3,
    "labels": ["bug"],
    "retry_minutes": [2, 4, 8, 16, 32, 60],
}
# The shared Claude 7-day window, as a percentage used. Review is what spends
# it: every delivery task ends in an independent Opus review. Above the pause
# percent review routes to the next reviewer with quota, and below the resume
# percent Opus comes back. The gap between the two stops the routing flapping
# across one threshold, and the margin below 100 leaves an operator a window
# they can still work inside. A policy that predates this block reads these,
# so the routing arrives without an operator re-post.
DEFAULT_QUOTA_GUARD = {
    "claude_7d_pause_percent": 85,
    "claude_7d_resume_percent": 75,
    # Quota about to be replaced is not quota worth preserving.
    "claude_7d_imminent_reset_minutes": 120,
}
# An observation older than this says nothing about now, and an unknown
# reading never starts a fallback: downgrading every review because a broker
# read failed would turn one outage into two.
QUOTA_GUARD_MAX_AGE_SECONDS = 3600
# Lane-wide routing, which is what the board renders. The per-node wait is
# deliberately NOT here: it is one task's node, recorded against that task, and
# reading it as the lane's verdict would say "no reviewer has quota" while
# another review was running.
REVIEWER_ROUTING_ACTIONS = (
    "reviewer_fallback",
    "reviewer_restored",
    "review_waiting",
    "quota_guard_unknown",
)
# One review node that cannot start yet, audited against its task.
REVIEW_NODE_WAITING_ACTION = "review_node_waiting"
# ADR agents/038 decision 5. A class carries a verification mode and a floor on
# the implementer tier, and judgment work never routes to the cheap lane.
# DEFAULT_TASK_CLASS is re-exported from factory_models, which owns it because
# the column default is written there.
# Machine-verified: an objective done condition a machine checks.
MACHINE_VERIFIED_CLASSES = ("bug-fix", "mechanical-refactor", "docs")
# Advisory: the output is a comment or a digest. No PR, no review gate.
ADVISORY_CLASSES = ("advisory-diagnosis", "advisory-triage", "refine")
# Judgment: correctness is only assessable by reading, so the floor is Opus.
JUDGMENT_CLASSES = ("judgment-analysis",)
TASK_CLASSES = MACHINE_VERIFIED_CLASSES + ADVISORY_CLASSES + JUDGMENT_CLASSES
# The two lanes a task runs in. Delivery work ends in a pull request an Opus
# reviewer has to read, and that review is the only input the factory is
# actually short of. Advisory work ends in a comment: it costs a cheap
# implementer and no review at all, so it is bounded separately rather than
# competing with delivery for one number.
LANES = ("delivery", "advisory")
# The identity autonomous intake writes its receipts under. It lives here
# rather than beside the loop because the daily cap counts those receipts and
# the count is read from here, by both the loop and the board. factory_intake
# re-exports it, so a caller that already knows the name still finds it there.
INTAKE_ACTOR = "factory:intake"
# Absent means one delivery task and no advisory work, which is the shape a
# policy written before lanes existed asked for. The advisory lane is opt-in:
# an operator who wants refine running says so.
DEFAULT_LANE_MAX_TASKS = {"delivery": 1, "advisory": 0}
# Nodes the reconciler may hold in flight for one task at once. One preserves
# the serial lane, so a policy written before fan-out existed never fans out.
DEFAULT_MAX_PARALLEL_NODES = 1
# Attempts the engine gives each node of a review round it inserts. A correction
# that fails is a deviation the planner must answer, not a turn to spend again,
# so a round costs exactly two turns and the reserve can say so honestly.
REVIEW_ROUND_ATTEMPTS = 1


def turn_reservation_usd(model: str, turn_class: str, fallback: float) -> float:
    """Reserve short decisions at the model's conservative turn-class price."""
    if turn_class in {"planner", "refine"} and model in {
        "astra",
        "spark",
        "pi-spark",
        "qwen",
        "gpt-6-astra",
        "muse-spark-1.3-contributor",
    }:
        return 0.50
    return float(fallback)


def review_reservation_usd(model: str, changed_lines: int, fallback: float) -> float:
    """Price review context and output, retaining the observed Opus floor."""
    from shared.pricing import price_usage

    lines = min(max(changed_lines, 0), 1_000_000) if type(changed_lines) is int else 0
    # Allow context exploration and reasoning beyond the patch itself.
    priced = price_usage(
        model,
        {"input_tokens": 50_000 + 20 * lines, "output_tokens": 10_000 + 2 * lines},
    )
    estimate = (
        float(fallback)
        if priced is None
        else math.ceil(priced.cost_usd * 4 * 100) / 100
    )
    floor = 8.0 if model == "opus" or model.startswith("claude-opus-") else 0.0
    return max(floor, estimate)


# Pools whose head must be the role's own configured model, so the policy's
# stated preference is always what a pool is ranked from.
_POOL_ROLES = {"conductor": "conductor_model", "worker": "worker_model"}
# Pools for a class of node rather than a configured role. They name no policy
# field, so there is no head to anchor them to; every member still has to be
# an allowed model. "implement" is delivery implementation work and defaults
# to the worker pool. "refine" is the advisory briefing node and defaults to
# Muse first, because a brief is read by a person and never merged.
# "reviewer" is the ordered fallback review runs down while the shared Claude
# window is nearly spent; it is a class pool rather than a role pool because
# its default need not start at the policy's own reviewer_model.
_CLASS_POOL_ROLES = ("implement", "refine", "reviewer")

# The escalation option vocabulary, shared by the two paths that raise one.
# A refine verdict of needs-human and a delivery planner's pause both hand a
# person the same kind of thing: two to four concrete acts with the
# recommendation first. It lives here rather than in factory_refine because
# factory_refine imports factory_conductor, so the conductor cannot import
# back the other way to reach it.
#
# What an option does when an operator picks it. Every effect except `hold`
# writes to GitHub, and `hold` exists so "leave it exactly as it is" is a
# choice a person can record rather than a tab they close.
OPTION_EFFECTS = (
    "agent-ready",
    "close",
    "supersede",
    "split",
    "defer",
    "hold",
)
# The one effect that puts the work back in front of the lane rather than
# ending it. A delivery escalation answered with it is re-admitted carrying
# the operator's answer as direction, and it is what `resume_task` applies,
# so a delivery pause is required to offer it first.
CONTINUE_EFFECT = "agent-ready"
# What each effect reads as in one word, for a prompt and for a comment. The
# operator page has its own copy of this in escalations-view.js.
EFFECT_WORD = {
    "agent-ready": "deliver",
    "close": "close",
    "supersede": "supersede",
    "split": "split",
    "defer": "defer",
    "hold": "hold",
}
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_SPLIT_CHILDREN = 5
CLOSE_REASONS = ("not_planned", "completed")
OPTION_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

OPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "label", "effect"],
    "properties": {
        "key": {"type": "string", "pattern": OPTION_KEY.pattern},
        "label": {"type": "string", "minLength": 1, "maxLength": 120},
        "effect": {"enum": list(OPTION_EFFECTS)},
        "detail": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "maxLength": 2000},
                "target": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["limit", "value"],
                    "properties": {
                        "limit": {
                            "enum": [
                                "task_budget",
                                "max_task_turns_hard",
                                "max_planner_turns",
                            ]
                        },
                        "value": {"type": "number", "minimum": 0},
                    },
                },
                "reason": {"enum": list(CLOSE_REASONS)},
                "comment": {"type": "string", "maxLength": 2000},
                "closes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {"type": "integer", "minimum": 1},
                },
                "in_favour_of": {"type": "integer", "minimum": 1},
                "children": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_SPLIT_CHILDREN,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "body"],
                        "properties": {
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 256,
                            },
                            "body": {"type": "string", "maxLength": 8000},
                        },
                    },
                },
            },
        },
    },
}


def verify_option(option: object, index: int) -> str | None:
    """One option's shape, checked by the server rather than by the schema.

    The schema goes to the guest so it writes the right thing; this runs on
    what came back, because the artifact is a claim until the server has
    checked it. Each effect is checked on the fields it will actually use at
    apply time, so an operator never clicks a button whose effect has nothing
    to act with.
    """
    where = f"option {index + 1}"
    if not isinstance(option, dict):
        return f"{where} is not an object"
    unknown = set(option) - {"key", "label", "effect", "detail"}
    if unknown:
        return f"{where} carries unsupported fields"
    key, label = option.get("key"), option.get("label")
    if not isinstance(key, str) or not OPTION_KEY.fullmatch(key):
        return f"{where} has no usable key"
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        return f"{where} has no usable label"
    effect = option.get("effect")
    if effect not in OPTION_EFFECTS:
        return f"{where} names no known effect"
    detail = option.get("detail")
    if detail is None:
        detail = {}
    if not isinstance(detail, dict):
        return f"{where} detail is not an object"
    if effect == "close" and detail.get("reason") not in CLOSE_REASONS:
        return f"{where} closes with no reason"
    if effect == "defer" and not str(detail.get("comment") or "").strip():
        return f"{where} defers with no wait condition"
    if effect == "supersede":
        closes = detail.get("closes")
        if not isinstance(closes, list) or not 1 <= len(closes) <= 10:
            return f"{where} supersedes with no issues to close"
        if any(type(number) is not int or number < 1 for number in closes):
            return f"{where} supersedes with an invalid issue number"
        if type(detail.get("in_favour_of")) is not int or (detail["in_favour_of"] < 1):
            return f"{where} supersedes with no issue in favour"
        if detail["in_favour_of"] in closes:
            return f"{where} supersedes the issue it favours"
    if effect == "split":
        children = detail.get("children")
        if not isinstance(children, list) or not 1 <= len(children) <= (
            MAX_SPLIT_CHILDREN
        ):
            return f"{where} splits into no children"
        for child in children:
            if not isinstance(child, dict):
                return f"{where} has a child that is not an object"
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                return f"{where} has a child with no title"
            if not isinstance(child.get("body", ""), str):
                return f"{where} has a child with a non-text body"
    return None


def verify_option_list(options: object, *, subject: str) -> str | None:
    """The bounded, distinct option list an escalation must carry.

    ``subject`` names what raised it, so the refusal an operator or a planner
    reads says which artifact was wrong rather than only that one was.
    """
    if not isinstance(options, list):
        return f"{subject} carries no options"
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"{subject} carries {len(options)} options, not two to four"
    for index, option in enumerate(options):
        invalid = verify_option(option, index)
        if invalid is not None:
            return invalid
    keys = [option["key"] for option in options]
    if len(set(keys)) != len(keys):
        return f"{subject} repeats an option key"
    return None


def factory_max_concurrent_tasks() -> int:
    """Hard ceiling on factory tasks in flight, over both lanes together.

    The chart owns it so a posted policy cannot open more lanes than the
    platform is sized for. It lives here rather than in factory.orchestration.config because
    the board reads it through status(), and the board deliberately links only
    this narrow library rather than the whole swarm package.
    """
    return max(1, int(os.environ.get("FACTORY_MAX_CONCURRENT_TASKS", "1")))


def factory_background_reserve() -> int:
    """Background session slots the factory leaves for everything else.

    The factory shares one background admission pool with the qwen drainer,
    the knowledge drainer and the synthetic probes, and none of those can wait
    the way a factory node can: a node the pool declines simply stays ready for
    the next tick. Raising the task ceiling to twelve without this would let
    delivery take every slot and leave the drainer with none.
    """
    return max(0, int(os.environ.get("FACTORY_BACKGROUND_RESERVE", "2")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def _money(value: object, name: str, *, zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    if value < 0 or (not zero and value == 0):
        raise ValueError(f"invalid {name}")
    return float(value)


def normalize_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise ValueError("invalid repo")
    if len(repo) > 256 or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("invalid repo")
    return repo.lower()


def validate_policy(policy: dict) -> dict:
    if (
        not isinstance(policy, dict)
        or not (_POLICY_KEYS - _OPTIONAL_POLICY_KEYS) <= set(policy) <= _POLICY_KEYS
    ):
        raise ValueError("policy must contain exactly the supported operator fields")
    result = dict(policy)
    if "reviewer_model" not in result:
        result["reviewer_model"] = policy["conductor_model"]
    result["repo"] = normalize_repo(policy["repo"])
    issues = policy["issue_numbers"]
    if not isinstance(issues, list) or not 1 <= len(issues) <= 100:
        raise ValueError("invalid issue_numbers")
    result["issue_numbers"] = sorted(
        {_integer(i, "issue_number", 1, 2**31 - 1) for i in issues}
    )
    result["max_tasks"] = _validate_max_tasks(policy["max_tasks"])
    for key, low, high in (
        ("generation", 0, 2**31 - 1),
        ("turn_timeout_seconds", 1, 43200),
        ("max_attempts", 1, 10),
        ("task_timeout_seconds", 1, 86400),
    ):
        result[key] = _integer(policy[key], key, low, high)
    # The accepted plan sizes the task; policy keeps only the envelope it must
    # fit inside. max_turns_per_task was the old fixed cap, so it is accepted
    # as that envelope and a live policy needs no re-post.
    if "max_turns_per_task" in policy:
        result["max_turns_per_task"] = _integer(
            policy["max_turns_per_task"], "max_turns_per_task", 1, 100
        )
    if "max_task_turns_hard" in policy:
        result["max_task_turns_hard"] = _integer(
            policy["max_task_turns_hard"], "max_task_turns_hard", 1, 500
        )
    elif "max_turns_per_task" in policy:
        result["max_task_turns_hard"] = result["max_turns_per_task"]
    else:
        raise ValueError("policy needs max_task_turns_hard or max_turns_per_task")
    # Absent means the serial lane this reconciler shipped with, so raising it
    # is an explicit operator act rather than a side effect of this change.
    result["max_parallel_nodes"] = _integer(
        policy.get("max_parallel_nodes", DEFAULT_MAX_PARALLEL_NODES),
        "max_parallel_nodes",
        1,
        8,
    )
    # Planning rounds are capped separately from delivery work. An absent field
    # means a policy written before the split, so it inherits the envelope
    # rather than forcing an operator to re-post a live policy.
    result["max_planner_turns"] = (
        _integer(policy["max_planner_turns"], "max_planner_turns", 1, 100)
        if "max_planner_turns" in policy
        else result["max_task_turns_hard"]
    )
    result["max_review_rounds"] = _integer(
        policy.get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS),
        "max_review_rounds",
        0,
        10,
    )
    result["max_review_recovery_rounds"] = _integer(
        policy.get("max_review_recovery_rounds", DEFAULT_MAX_REVIEW_RECOVERY_ROUNDS),
        "max_review_recovery_rounds",
        0,
        2,
    )
    for key in ("task_budget_usd", "turn_budget_usd"):
        result[key] = _money(policy[key], key)
    if result["turn_budget_usd"] > result["task_budget_usd"]:
        raise ValueError("turn budget exceeds task budget")
    models = policy["allowed_models"]
    if not isinstance(models, list) or not 1 <= len(models) <= 16:
        raise ValueError("invalid allowed_models")
    result["allowed_models"] = sorted({_text(m, "model", 128) for m in models})
    if policy["conductor_model"] not in result["allowed_models"]:
        raise ValueError("conductor model is not allowed")
    if result["reviewer_model"] not in result["allowed_models"]:
        raise ValueError("reviewer model is not allowed")
    if policy["worker_model"] not in result["allowed_models"]:
        raise ValueError("worker model is not allowed")
    if "model_pools" in policy:
        result["model_pools"] = _validate_model_pools(policy["model_pools"], result)
    result["intake"] = _validate_intake(policy.get("intake", {}))
    result["problem_issues"] = _validate_problem_issues(
        policy.get("problem_issues", {})
    )
    result["quota_guard"] = _validate_quota_guard(policy.get("quota_guard", {}))
    # Landing is the one factory step that writes to the repository rather
    # than reading it, so it is a flag of its own and defaults off. A policy
    # written before landing existed reads as false and lands nothing.
    auto_merge = policy.get("auto_merge", DEFAULT_AUTO_MERGE)
    if type(auto_merge) is not bool:
        raise ValueError("invalid auto_merge")
    result["auto_merge"] = auto_merge
    if result["turn_timeout_seconds"] > result["task_timeout_seconds"]:
        raise ValueError("turn timeout exceeds task timeout")
    result["base_branch"] = _text(policy["base_branch"], "base_branch", 256)
    return result


def _validate_intake(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= set(DEFAULT_INTAKE):
        raise ValueError("invalid intake")
    result = {}
    for key in ("enabled", "refine_enabled", "close_enabled"):
        setting = value.get(key, DEFAULT_INTAKE[key])
        if type(setting) is not bool:
            raise ValueError(f"invalid {key}")
        result[key] = setting
    for key in ("labels", "exclude_labels"):
        labels = value.get(key, DEFAULT_INTAKE[key])
        if not isinstance(labels, list) or len(labels) > 32:
            raise ValueError(f"invalid {key}")
        result[key] = sorted({_text(label, "label", 128) for label in labels})
    # The cap bounds delivery churn, which is pull requests and the Opus
    # reviews they cost, so the ceiling is high enough that an operator can
    # take it out of the way of a burn-down rather than have it throttle one.
    result["max_per_day"] = _integer(
        value.get("max_per_day", DEFAULT_INTAKE["max_per_day"]),
        "max_per_day",
        1,
        10000,
    )
    result["cooldown_hours"] = _integer(
        value.get("cooldown_hours", DEFAULT_INTAKE["cooldown_hours"]),
        "cooldown_hours",
        1,
        168,
    )
    result["max_closes_per_day"] = _integer(
        value.get("max_closes_per_day", DEFAULT_INTAKE["max_closes_per_day"]),
        "max_closes_per_day",
        1,
        50,
    )
    # Selection lowercases both sides, so an overlap that only differs in
    # case is the same contradiction and must be refused here too.
    if {label.lower() for label in result["labels"]} & {
        label.lower() for label in result["exclude_labels"]
    }:
        raise ValueError("intake labels overlap exclude_labels")
    return result


def _validate_problem_issues(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= set(DEFAULT_PROBLEM_ISSUES):
        raise ValueError("invalid problem_issues")
    result = dict(DEFAULT_PROBLEM_ISSUES)
    enabled = value.get("enabled", result["enabled"])
    if type(enabled) is not bool:
        raise ValueError("invalid problem_issues enabled")
    result["enabled"] = enabled

    sources = value.get("sources", result["sources"])
    if not isinstance(sources, dict) or not set(sources) <= set(PROBLEM_ISSUE_SOURCES):
        raise ValueError("invalid problem_issues sources")
    result["sources"] = dict(DEFAULT_PROBLEM_ISSUES["sources"])
    for source, source_enabled in sources.items():
        if type(source_enabled) is not bool:
            raise ValueError(f"invalid problem_issues source {source}")
        result["sources"][source] = source_enabled

    for key, low, high in (
        ("source_audit_limit", 1, 50),
        ("issue_pages", 1, 2),
        ("issues_per_page", 1, 100),
        ("max_per_24_hours", 1, 100),
    ):
        result[key] = _integer(value.get(key, result[key]), key, low, high)
    result["max_per_tick"] = _integer(
        value.get("max_per_tick", result["max_per_tick"]),
        "max_per_tick",
        1,
        1,
    )
    labels = value.get("labels", result["labels"])
    if labels != ["bug"]:
        raise ValueError("problem_issues labels must be exactly bug")
    result["labels"] = ["bug"]
    retries = value.get("retry_minutes", result["retry_minutes"])
    if retries != DEFAULT_PROBLEM_ISSUES["retry_minutes"]:
        raise ValueError("invalid problem_issues retry_minutes")
    result["retry_minutes"] = list(DEFAULT_PROBLEM_ISSUES["retry_minutes"])
    return result


def _validate_quota_guard(value: object) -> dict:
    if not isinstance(value, dict) or not set(value) <= set(DEFAULT_QUOTA_GUARD):
        raise ValueError("invalid quota_guard")
    result = {
        key: _integer(
            value.get(key, DEFAULT_QUOTA_GUARD[key]),
            key,
            0 if key == "claude_7d_imminent_reset_minutes" else 1,
            10080 if key == "claude_7d_imminent_reset_minutes" else 100,
        )
        for key in DEFAULT_QUOTA_GUARD
    }
    # Equal thresholds are a flap, not a guard: the lane would pause and resume
    # on alternate readings of the same number.
    if result["claude_7d_resume_percent"] >= result["claude_7d_pause_percent"]:
        raise ValueError("quota_guard resume percent must be below pause percent")
    return result


def quota_guard_policy(policy: dict) -> dict:
    """The guard block, defaulted, so a policy stored before it still guards."""
    return _validate_quota_guard(policy.get("quota_guard") or {})


def intake_policy(policy: dict) -> dict:
    """The intake block, defaulted, so a policy stored before it reads as off."""
    return _validate_intake(policy.get("intake") or {})


def problem_issues_policy(policy: dict) -> dict:
    """The exact-event issue producer block, defaulted fully off."""
    return _validate_problem_issues(policy.get("problem_issues") or {})


def _policy_for_generation_comparison(policy: dict) -> dict:
    """Default the new inert block before comparing active policies.

    Policies stored before the producer existed omit this key. A briefly
    accepted nullable representation is equivalent to omission too. Neither
    should make an otherwise identical configure retry require a new
    generation while work is active.
    """
    comparable = dict(policy)
    comparable["problem_issues"] = problem_issues_policy(comparable)
    return comparable


def auto_merge_enabled(policy: dict) -> bool:
    """Whether this policy lets the lane arm a merge.

    A stored policy written before landing existed has no key at all, and a
    malformed one is not a licence: anything that is not exactly True reads as
    off, because the failure that matters here writes to the repository.
    """
    return policy.get("auto_merge", DEFAULT_AUTO_MERGE) is True


def validate_task_class(value: object) -> str:
    if value not in TASK_CLASSES:
        raise ValueError("invalid task_class")
    return value


def receipt_task_class(row) -> str:
    """A receipt written before classes existed is an ordinary bug fix."""
    return row.task_class or DEFAULT_TASK_CLASS


def is_advisory(task_class: str) -> bool:
    """Advisory work comments and never delivers, so it admits no DAG."""
    return task_class in ADVISORY_CLASSES


def lane_for(task_class: str) -> str:
    """The lane a class runs in. Lane follows class; it is not configurable.

    A task that ends in a comment is advisory and a task that ends in a pull
    request is delivery, and which of those a class is was already decided by
    its verification mode. Letting an operator move a class between lanes
    would let advisory concurrency buy delivery work that no reviewer is
    sized for.
    """
    return "advisory" if is_advisory(task_class) else "delivery"


def _validate_max_tasks(value: object) -> dict:
    """Per-lane concurrency. A bare integer is the delivery lane, as before.

    A policy posted before lanes existed carries one number, which asked for
    that many tasks in flight and said nothing about advisory work. Reading
    it as the delivery lane keeps exactly the capacity it asked for, and the
    advisory lane stays shut until an operator opens it.
    """
    if isinstance(value, dict):
        if not value or not set(value) <= set(LANES):
            raise ValueError("invalid max_tasks")
        return {
            "delivery": _integer(
                value.get("delivery", DEFAULT_LANE_MAX_TASKS["delivery"]),
                "max_tasks.delivery",
                1,
                100,
            ),
            "advisory": _integer(
                value.get("advisory", DEFAULT_LANE_MAX_TASKS["advisory"]),
                "max_tasks.advisory",
                0,
                100,
            ),
        }
    return {
        "delivery": _integer(value, "max_tasks", 1, 100),
        "advisory": DEFAULT_LANE_MAX_TASKS["advisory"],
    }


def lane_max_tasks(policy: dict) -> dict:
    """What the policy asks for per lane, before the chart ceiling applies."""
    return _validate_max_tasks(
        policy.get("max_tasks", DEFAULT_LANE_MAX_TASKS["delivery"])
    )


def lane_limits(policy: dict) -> dict:
    """Per-lane concurrency with the chart ceiling applied to the SUM.

    The chart owns one number for the whole factory, so the lanes divide it
    rather than each getting it. Delivery is served first and keeps at least
    one slot: a ceiling an operator set below the policy must never leave the
    delivery lane unable to start anything. Advisory takes what is left.
    """
    ceiling = factory_max_concurrent_tasks()
    wanted = lane_max_tasks(policy)
    delivery = max(1, min(wanted["delivery"], ceiling))
    advisory = max(0, min(wanted["advisory"], ceiling - delivery))
    return {"delivery": delivery, "advisory": advisory}


def lane_usage(policy: dict, receipts: list[dict]) -> dict:
    """Per-lane limits beside what is in flight and what is waiting.

    Active tasks occupy capacity across policy generations. Only queued
    receipts are generation-scoped, because older queues cannot be admitted.
    """
    limits = lane_limits(policy)
    usage = {lane: {"limit": limits[lane], "active": 0, "queued": 0} for lane in LANES}
    for receipt in receipts:
        lane = receipt.get("routing_tier") or lane_for(
            receipt.get("task_class") or DEFAULT_TASK_CLASS
        )
        if receipt.get("state") in _ACTIVE:
            usage[lane]["active"] += 1
        elif is_current_generation_queue(policy, receipt):
            usage[lane]["queued"] += 1
    return usage


def is_current_generation_queue(policy: dict, receipt: object) -> bool:
    """Whether a receipt belongs to the only queue admission can consume."""
    generation = policy.get("generation", 0)
    if isinstance(receipt, dict):
        state = receipt.get("state")
        receipt_generation = receipt.get("generation", 0)
    else:
        state = getattr(receipt, "state", None)
        receipt_generation = getattr(receipt, "generation", 0)
    return state == "queued" and receipt_generation == generation


def _validate_model_pools(pools: object, policy: dict) -> dict:
    """Each pool is an ordered preference list of allowed models.

    A role pool is headed by that role's own configured model, so the policy's
    stated preference is what the pool is ranked from. A class pool names no
    policy field and so has no head to anchor; its order is the preference.
    """
    known = set(_POOL_ROLES) | set(_CLASS_POOL_ROLES)
    if not isinstance(pools, dict) or not pools or not set(pools) <= known:
        raise ValueError("invalid model_pools")
    result = {}
    for role, pool in pools.items():
        if not isinstance(pool, list) or not 1 <= len(pool) <= 8:
            raise ValueError(f"invalid model_pools.{role}")
        models = [_text(m, "model", 128) for m in pool]
        if len(set(models)) != len(models):
            raise ValueError(f"duplicate model in model_pools.{role}")
        if role in _POOL_ROLES and models[0] != policy[_POOL_ROLES[role]]:
            raise ValueError(f"model_pools.{role} must start with the {role} model")
        if any(m not in policy["allowed_models"] for m in models):
            raise ValueError(f"model_pools.{role} names a model that is not allowed")
        result[role] = models
    return result


@contextmanager
def _read_session(session: Session | None = None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with Session(get_engine()) as owned:
            yield owned


@contextmanager
def _locked_session(
    session: Session | None = None,
) -> Iterator[tuple[Session, FactoryControl]]:
    """Supplied sessions retain their transaction for atomic caller composition."""
    with _read_session(session) as db:
        try:
            result = db.execute(
                update(FactoryControl)
                .where(FactoryControl.id == "factory")
                .values(version=FactoryControl.version)
            )
            if result.rowcount != 1:
                raise RuntimeError("factory control migration/seed is missing")
            control = db.exec(
                select(FactoryControl)
                .where(FactoryControl.id == "factory")
                .execution_options(populate_existing=True)
            ).one()
            yield db, control
            if session is None:
                db.commit()
            else:
                db.flush()
        except BaseException:
            if session is None:
                db.rollback()
            raise


def _audit(
    db: Session,
    actor: str,
    action: str,
    *,
    task_id: str | None = None,
    **detail: object,
) -> None:
    if task_id is not None and db.get(SwarmTask, task_id) is None:
        detail["requested_task_id"] = task_id
        task_id = None
    db.add(
        FactoryAudit(
            actor=actor, action=action, task_id=task_id, detail_json=_json(detail)
        )
    )


def _receipt(db: Session, task_id: str) -> FactoryReceipt | None:
    return db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.task_id == task_id)
        .execution_options(populate_existing=True)
    ).first()


def _starts(db: Session, task_id: str) -> list[FactoryStart]:
    return list(
        db.exec(
            select(FactoryStart)
            .where(FactoryStart.task_id == task_id)
            .order_by(FactoryStart.id)
            .execution_options(populate_existing=True)
        ).all()
    )


# Start keys are "factory-node:<task id>:<node key>:<attempt>". A node key may
# itself contain colons, so the attempt and the two fixed leading segments are
# what anchor the parse.
_START_KEY = re.compile(r"^factory-node:[^:]+:(?P<node_key>.+):[0-9]+$")


def _planner_key(start_key: str | None) -> bool:
    """True for a conductor planning round rather than a unit of task work.

    An unparsable key counts as work, so the turn cap can never be widened by
    a start whose shape this cannot read.
    """
    match = _START_KEY.match(start_key or "")
    return bool(match) and match.group("node_key").startswith("conductor_")


def _planner_start(row: FactoryStart) -> bool:
    return _planner_key(row.start_key)


def task_turn_ceiling(policy: dict) -> int:
    """The work-turn envelope no accepted plan may size past.

    A receipt pins its policy at admission, so a task admitted before
    max_task_turns_hard existed reads its old fixed cap as the envelope.
    """
    ceiling = policy.get("max_task_turns_hard")
    return policy["max_turns_per_task"] if ceiling is None else ceiling


def parallel_limit(policy: dict) -> int:
    """Nodes one task may hold in flight at once, serial unless raised."""
    limit = policy.get("max_parallel_nodes")
    return DEFAULT_MAX_PARALLEL_NODES if limit is None else limit


def planner_turn_cap(policy: dict) -> int:
    """The planning round cap, inherited from the envelope by older policies.

    A receipt pins its policy at admission, so a task admitted before
    max_planner_turns existed still needs a bound.
    """
    cap = policy.get("max_planner_turns")
    return task_turn_ceiling(policy) if cap is None else cap


def allowance_from_graph(
    nodes: list[dict],
    runs: list[dict],
    policy: dict,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    graph_revision: int,
    reviewable: bool | None = None,
) -> dict:
    """Size a task from the plan it accepted, not from a fixed policy number.

    Work turns are one per attempt the live graph can still spend, plus every
    work turn history already spent, plus the nodes the engine may still insert
    on its own. Review rounds are reserved lazily: only the next one, and at the
    two turns it really costs, a correction and a re-review each inserted at
    ``REVIEW_ROUND_ATTEMPTS``. Reserving every remaining round up front priced a
    loop the task would probably never open, and it made a modest envelope
    unable to hold its own plan. The allowance instead grows by one round at a
    time, as each round is inserted and the next one is reserved behind it, and
    every insertion is checked against the envelope as it happens. A fan-in is
    one node at the policy's ``max_attempts``, exactly as the inserted node will
    carry, so a fan-in stays turn-neutral. Review rounds are reserved only when
    the plan holds a review node that could open one.

    An attempt already spent is counted in history, so its node slot is not
    counted again; before anything runs the two readings agree. Dollars are the
    same shape in money: charged history plus every live unsucceeded node's
    unspent ceiling, plus the same prospective insertions, each at one per-turn
    ceiling because a node's ceiling is shared across its attempts.

    A discarded node stops contributing its remaining slots, and its spent
    attempts stay in history, so discarding never refunds a consumed turn.

    ``reviewable`` overrides whether these nodes could open a round at all. A
    refusal reads the live graph under the reserve the refused edit implies, so
    an edit that adds the first review node is sized against the round that
    review would open rather than against a graph that has none.
    """
    attempts: dict[str, int] = {}
    charged: dict[str, float] = {}
    succeeded: set[str] = set()
    work_turns_used = 0
    charged_total = 0.0
    denied: dict[str, int] = {}
    for run in runs:
        key = run["node_key"]
        cost = float(run["accounted_cost_usd"])
        charged[key] = charged.get(key, 0.0) + cost
        charged_total += cost
        if run.get("capacity_denied"):
            seen = denied.get(key, 0)
            denied[key] = seen + 1
            if seen < MAX_CAPACITY_DENIED_ATTEMPTS:
                continue
        attempts[key] = attempts.get(key, 0) + 1
        if run["status"] == "succeeded":
            succeeded.add(key)
        if not key.startswith("conductor_"):
            work_turns_used += 1
    remaining_turns = 0
    remaining_usd = 0.0
    for node in nodes:
        key = node["node_key"]
        if key in succeeded:
            continue
        remaining_usd += max(0.0, node["max_cost_usd"] - charged.get(key, 0.0))
        if key.startswith("conductor_"):
            continue
        remaining_turns += max(0, node["max_attempts"] - attempts.get(key, 0))
    if reviewable is None:
        reviewable = any(node["node_key"].startswith("review_") for node in nodes)
    rounds = min(1, max(0, review_rounds_remaining)) if reviewable else 0
    fan_ins = max(0, fan_ins_remaining)
    from factory.orchestration.model_pool import pool_for

    review_cost = review_reservation_usd(
        pool_for("reviewer", policy)[0], 0, policy["turn_budget_usd"]
    )
    reserved_usd = (
        rounds * (policy["turn_budget_usd"] + review_cost)
        + fan_ins * policy["turn_budget_usd"]
    )
    reserved_turns = (
        2 * rounds * REVIEW_ROUND_ATTEMPTS + fan_ins * policy["max_attempts"]
    )
    return {
        "turns": work_turns_used + remaining_turns + reserved_turns,
        "usd": round(
            charged_total + remaining_usd + reserved_usd,
            6,
        ),
        "graph_revision": graph_revision,
        "review_rounds_reserved": rounds,
        "fan_ins_reserved": fan_ins,
        "derived": True,
    }


def derive_allowance(
    task_id: str,
    policy: dict,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    session: Session | None = None,
) -> dict:
    """Read the live graph and size the task from it."""
    from factory.orchestration import graph

    with _read_session(session) as db:
        return allowance_from_graph(
            graph.load_graph(task_id, session=db),
            graph.node_runs(task_id, session=db),
            policy,
            review_rounds_remaining=review_rounds_remaining,
            fan_ins_remaining=fan_ins_remaining,
            graph_revision=graph.current_version(task_id, session=db),
        )


def envelope_excess(
    allowance: dict, policy: dict, *, accounted: dict | None = None
) -> dict | None:
    """Name what a derived allowance overspends, or None when it fits.

    ``accounted`` is what the task is already sized at, before the refused
    edit, under the reserve that edit implies. Given it, the refusal also
    carries ``spare_turns`` and ``spare_usd``, what the envelope would still
    fund, so the planner can size a smaller edit against whichever of the two
    is binding instead of re-proposing the one that was just refused.
    """
    turns_allowed = task_turn_ceiling(policy)
    usd_allowed = policy["task_budget_usd"]
    if allowance["turns"] <= turns_allowed and allowance["usd"] <= usd_allowed:
        return None
    excess = {
        "turns": {"needed": allowance["turns"], "allowed": turns_allowed},
        "usd": {"needed": allowance["usd"], "allowed": usd_allowed},
    }
    if accounted is not None:
        excess["spare_turns"] = max(0, turns_allowed - accounted["turns"])
        excess["spare_usd"] = round(max(0.0, usd_allowed - accounted["usd"]), 6)
    return excess


def _stored_allowance(row: FactoryReceipt, policy: dict) -> dict:
    """The plan-derived allowance, or the envelope until a plan derives one.

    A task admitted before this column existed, or one whose planner has not
    produced an accepted plan yet, has nothing derived. The envelope is the
    honest fallback: it is what the old fixed cap already meant.
    """
    if row.allowance_json:
        stored = json.loads(row.allowance_json)
        if isinstance(stored, dict) and type(stored.get("turns")) is int:
            return stored
    return {
        "turns": task_turn_ceiling(policy),
        "usd": policy["task_budget_usd"],
        "graph_revision": None,
        "review_rounds_reserved": 0,
        "fan_ins_reserved": 0,
        "derived": False,
    }


def task_allowance(task_id: str, *, session: Session | None = None) -> dict:
    """The allowance currently stored for a task, or the envelope standing in."""
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        if row is None or not row.policy_json:
            return {"turns": 0, "usd": 0.0, "graph_revision": None, "derived": False}
        return _stored_allowance(row, _effective_policy(db, row))


def _effective_policy(db, row):
    from factory.orchestration.factory_funding_limits import effective_policy

    return effective_policy(db, row)


def continuation_grant(task_id: str, *, session: Session | None = None) -> dict | None:
    """Read the one permanent grant, independently of current rollout flags."""
    with _read_session(session) as db:
        from factory.orchestration.factory_funding_limits import amendment

        if amendment(db, task_id):
            return None
        row = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "continuation_granted",
            )
            .order_by(FactoryAudit.id)
        ).first()
        return json.loads(row.detail_json) if row else None


def record_allowance(
    task_id: str,
    policy: dict,
    actor: str,
    *,
    review_rounds_remaining: int,
    fan_ins_remaining: int = 0,
    cause: str | None = None,
    session: Session | None = None,
) -> dict:
    """Recompute and persist the allowance the current graph revision implies."""
    actor = _text(actor, "actor")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        allowance = derive_allowance(
            task_id,
            policy,
            review_rounds_remaining=review_rounds_remaining,
            fan_ins_remaining=fan_ins_remaining,
            session=db,
        )
        # Acceptance refuses an over-envelope plan, so this can only clamp a
        # graph that reached the server another way. Clamping never widens the
        # envelope after the fact.
        grant = continuation_grant(task_id, session=db)
        ceiling = grant["work_turn_ceiling"] if grant else task_turn_ceiling(policy)
        allowance["turns"] = min(allowance["turns"], ceiling)
        allowance["usd"] = min(allowance["usd"], policy["task_budget_usd"])
        row.allowance_json = _json(allowance)
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            actor,
            "allowance_derived",
            task_id=task_id,
            cause=cause,
            allowance=allowance,
        )
        return {"ok": True, "allowance": allowance}


# A start settled on one of these bases commits nothing. Its attempt is proven
# never to have reached a model, which is the same evidence the graph books at
# zero, so charging its reserved ceiling here would refuse the retry the graph
# just released.
FREE_START_BASES = ("no_model_post", "capacity_denied", "no_session_created")


def _start_node_key(row: FactoryStart) -> str | None:
    """The node this start belongs to, or None when the key does not parse."""
    match = _START_KEY.match(row.start_key or "")
    return match.group("node_key") if match else None


def _committed_cost(row: FactoryStart) -> float:
    if row.accounting_basis in FREE_START_BASES:
        return 0.0
    # Known completion without a provider cost consumes the reserved ceiling.
    if row.status in ("reserved", "uncertain"):
        return max(row.max_cost_usd, row.cost_usd or 0)
    return row.max_cost_usd if row.cost_usd is None else row.cost_usd


def _accounting(starts: list[FactoryStart]) -> dict:
    # Planner rounds read evidence and decide; they do not do the task's work.
    # Counting them against max_turns_per_task exhausts a task before it has
    # spent its allowance on delivery. They still consume budget.
    #
    # A capacity denial spends neither: the control plane refused the slot, so
    # no turn of either kind happened. Excused up to the same per node bound
    # graph.attempts_spent applies, so the turn gate and the attempt gate agree
    # on how many attempts a node has left.
    planner = 0
    turns = 0
    committed = 0.0
    unresolved = 0
    denied: dict[str | None, int] = {}
    # Sorted so the bound excuses the earliest denials whatever order the rows
    # were read in. An unsaved row sorts last; only settled rows carry a basis.
    for row in sorted(starts, key=lambda row: (row.id is None, row.id or 0)):
        committed += _committed_cost(row)
        unresolved += row.status in ("reserved", "uncertain")
        if row.accounting_basis == "capacity_denied":
            node_key = _start_node_key(row)
            seen = denied.get(node_key, 0)
            denied[node_key] = seen + 1
            if seen < MAX_CAPACITY_DENIED_ATTEMPTS:
                continue
        if _planner_start(row):
            planner += 1
        else:
            turns += 1
    return {
        "turns_used": turns,
        "planner_turns_used": planner,
        "committed_cost_usd": committed,
        "unresolved_starts": unresolved,
    }


def _start_dict(row: FactoryStart) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "task_id",
            "start_key",
            "actor",
            "model",
            "max_cost_usd",
            "status",
            "cost_usd",
            "accounting_basis",
            "session_id",
            "created_at",
        )
    }


def _recovery_deadline(db: Session, task_id: str, ordinary: datetime) -> datetime:
    event = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action == "landing_recovery_requested",
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    value = json.loads(event.detail_json).get("deadline_at") if event else None
    return datetime.fromisoformat(value) if value else ordinary


def _snapshot(db: Session, row: FactoryReceipt, *, body: bool = False) -> dict:
    starts = _starts(db, row.task_id) if row.task_id else []
    result = {
        key: getattr(row, key)
        for key in (
            "id",
            "repo",
            "issue_number",
            "work_item_id",
            "generation",
            "title",
            "url",
            "state",
            "task_id",
            "task_paused",
            "cancellation_requested",
        )
    }
    result["task_class"] = receipt_task_class(row)
    result["routing_tier"] = row.routing_tier or lane_for(result["task_class"])
    # The escalation document, when this receipt raised one. It is read by the
    # board, by the decision endpoint, and by the next refine prompt when the
    # operator asked for more, so it belongs on the one snapshot they share.
    result["escalation"] = (
        json.loads(row.escalation_json) if row.escalation_json else None
    )
    # The answer an operator gave to the previous task's escalation. The
    # planner prompt of the task this receipt was re-admitted for reads it on
    # its first round, which is the whole point of recording it.
    result["direction"] = json.loads(row.direction_json) if row.direction_json else None
    # Every task this receipt has already spent on the same issue. A decision
    # that re-admits an escalated delivery clears task_id, policy and
    # allowance so the next admission mints a fresh task, which would
    # otherwise take the escalated attempt's whole cost off the board.
    #
    # Deliberately NOT folded into turns_used or committed_cost_usd. Those are
    # measured against the current task's own allowance, and adding a previous
    # attempt's spend to them would read as a task over its budget on its
    # first turn and trip every limit in `limits` before a node had run.
    result["previous_task_ids"] = list(
        (result["direction"] or {}).get("previous_task_ids") or []
    )
    result["previous_attempts"] = [
        {"task_id": previous, **_accounting(_starts(db, previous))}
        for previous in result["previous_task_ids"]
    ]
    result["previous_spend"] = {
        "turns_used": sum(
            attempt["turns_used"] for attempt in result["previous_attempts"]
        ),
        "committed_cost_usd": sum(
            attempt["committed_cost_usd"] for attempt in result["previous_attempts"]
        ),
        "attempts": len(result["previous_attempts"]),
    }
    result.update(
        policy=_effective_policy(db, row) if row.policy_json else None,
        starts=[_start_dict(s) for s in starts],
        **_accounting(starts),
    )
    if row.task_id:
        # Bounded lifecycle evidence on the existing task/status surface. Never
        # return raw stop identity hashes, turn bodies or execution credentials.
        stop_rows = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action.in_(
                    (
                        "stop_intent",
                        "stop_request",
                        "stop_accepted",
                        "stop_observation",
                        # Absence observations are the evidence behind a held
                        # slot that is on its way to releasing, so the board
                        # shows "absent, 2 of 3" rather than nothing at all.
                        "stop_absence",
                        # Its counterpart. Without this the board renders one
                        # unbroken absence run where the guest actually
                        # answered in between.
                        "stop_presence",
                        "stop_settled",
                        "attempt_stop_requested",
                        "attempt_stop_cancel",
                    )
                ),
            )
            .order_by(FactoryAudit.id.desc())
            .limit(16)
        ).all()
        result["stop_events"] = []
        for event in stop_rows:
            detail = json.loads(event.detail_json)
            identity = detail.get("identity") or {}
            result["stop_events"].append(
                {
                    "action": event.action,
                    "actor": event.actor,
                    "request_key": detail.get("request_key"),
                    "created_at": event.created_at.isoformat(),
                    "workflow_id": detail.get("workflow_id"),
                    "session_id": detail.get("session_id", identity.get("session_id")),
                    "guest_id": detail.get("guest_id", identity.get("guest_id")),
                    **{
                        key: detail[key]
                        for key in (
                            "reason",
                            "error",
                            "request_number",
                            # Which absence reading this is, so a held slot on
                            # its way to releasing reads as "2 of 3" rather
                            # than as a row with no detail at all.
                            "observation",
                            "observations",
                            "retry_kind",
                            "retry_sample",
                            "retry_exhausted",
                            "retry_resolved",
                            "resolution",
                            "retry_started_at",
                            "retry_observed_at",
                            "retry_deadline_at",
                            "refusal",
                            "missing_proof",
                            "node_key",
                            "attempt",
                            "identity_sha256",
                            "cessation_confirmed",
                            "intervention_required",
                        )
                        if key in detail
                    },
                }
            )
        task = db.get(SwarmTask, row.task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        deadline = admitted + timedelta(
            seconds=result["policy"]["task_timeout_seconds"]
        )
        deadline = _recovery_deadline(db, row.task_id, deadline)
        allowance = _stored_allowance(row, result["policy"])
        result["allowance"] = allowance
        result.update(
            admitted_at=admitted.isoformat(),
            deadline_at=deadline.isoformat(),
            limits={
                "deadline_expired": _now() >= deadline,
                "turn_limit_reached": result["turns_used"] >= allowance["turns"],
                "planner_turn_limit_reached": result["planner_turns_used"]
                >= planner_turn_cap(result["policy"]),
                "budget_limit_reached": result["committed_cost_usd"]
                >= result["policy"]["task_budget_usd"],
            },
        )
        last = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "finish_task",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        result["evidence"] = (
            json.loads(last.detail_json).get("evidence") if last else None
        )
    if body:
        result["body"] = row.body
    return result


def delivery_admissions(db, since: datetime) -> int:
    """Delivery receipts autonomous intake opened since ``since``.

    This is what ``max_per_day`` bounds. The cap exists to bound delivery
    churn, which is pull requests and the Opus reviews they cost; an advisory
    refine writes a comment for cents and buys no review at all, so counting
    one against the cap would let cheap work throttle a burn-down. Counting
    receipts rather than ``intake_admitted`` audits is what makes the class
    readable at all: the audit trail has no class-scoped query, the receipt
    carries the class it was received under, and a receipt is written for
    exactly the admissions the audits record.

    Scoped to intake's own actor. An operator who names issues in the policy
    allowlist has already decided how many to take, and reading their receipts
    as autonomous admissions would close the lane on them.
    """
    return len(
        db.exec(
            select(FactoryReceipt.id).where(
                FactoryReceipt.actor == INTAKE_ACTOR,
                FactoryReceipt.created_at >= since,
                # A receipt written before classes existed reads as the
                # default, which is delivery, so an untyped row still counts.
                or_(
                    FactoryReceipt.routing_tier == "delivery",
                    FactoryReceipt.routing_tier.is_(None)
                    & or_(
                        FactoryReceipt.task_class.is_(None),
                        FactoryReceipt.task_class.notin_(ADVISORY_CLASSES),
                    ),
                ),
            )
        ).all()
    )


# The escape hatch every unresolved escalation carries. These three are
# appended by the view rather than written by a brief, so an operator who
# agrees with none of the offered options can still leave the card without a
# chat round trip and a wait. Their keys are namespaced with a colon, which
# the option key pattern in factory_refine forbids, so a brief can never
# author a key that collides with one of these.
ESCAPE_OPTIONS = (
    {
        "key": "escape:close",
        "label": "Close the issue",
        "effect": "escape-close",
        "detail": {"reason": "not_planned"},
    },
    {
        "key": "escape:defer",
        "label": "Defer it",
        "effect": "escape-defer",
        "detail": {},
    },
    {
        "key": "escape:dismiss",
        "label": "Dismiss the escalation",
        "effect": "escape-dismiss",
        "detail": {},
    },
)

# The one effect that records a resolution without ending the conversation.
# A dismiss writes nothing to GitHub: the issue is exactly as it was, still
# carrying `needs-human`, and the only thing that changed is that the card
# left the operator's list. Treating it as final would make one keypress the
# end of the matter, so it is the one resolution a later decision, a chat
# request, or a fresh brief is allowed to land over.
NON_TERMINAL_EFFECTS = ("escape-dismiss",)


def issue_body_hash(body: object) -> str:
    """Hash issue text after collapsing changes that carry no new words."""
    text = body if isinstance(body, str) else ""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def terminal_effect(effect: str | None) -> bool:
    """Whether an effect closes the escalation for good."""
    return effect not in NON_TERMINAL_EFFECTS


def terminal_resolution(resolved: dict | None) -> bool:
    """Whether a recorded resolution closes the escalation for good."""
    return resolved is not None and terminal_effect(resolved.get("effect"))


def decision_identity(receipt: dict) -> str | None:
    """Identify the exact brief, including hidden option effects and context.

    Resolution is excluded so acknowledging an answer does not change its
    identity. Brief writers retain their source task, even after re-admission
    clears the receipt's task pointer. Legacy documents remain readable; their
    next persisted brief gains that source identity without a data migration.
    """
    escalation = receipt.get("escalation")
    if not escalation:
        return None
    payload = {
        "receipt_id": receipt.get("id"),
        "repo": receipt.get("repo"),
        "generation": receipt.get("generation"),
        "brief": {key: value for key, value in escalation.items() if key != "resolved"},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "decision:" + hashlib.sha256(encoded).hexdigest()


def escalation_view(receipt: dict) -> dict | None:
    """One escalation as the operator page renders it, from a board snapshot.

    Pure shaping over the snapshot dict so the board, the public snapshot job
    and the tests all read the same thing without a second database pass.
    """
    escalation = receipt.get("escalation")
    if not escalation:
        return None
    resolved = escalation.get("resolved")
    return {
        "receipt_id": receipt.get("id"),
        "decision_id": decision_identity(receipt),
        "repo": receipt.get("repo"),
        "issue_number": receipt.get("issue_number"),
        "title": receipt.get("title"),
        "url": receipt.get("url"),
        "task_class": receipt.get("task_class"),
        "generation": receipt.get("generation"),
        "state": receipt.get("state"),
        # Which path raised it. A refine escalation is a briefing asking what
        # the work should be; a delivery escalation is a planner mid-task
        # asking a question it cannot answer, and it names a branch and
        # usually a pull request the next attempt can carry on from.
        "kind": receipt.get("routing_tier")
        or lane_for(receipt.get("task_class") or DEFAULT_TASK_CLASS),
        "recommendation": escalation.get("recommendation"),
        "question": escalation.get("question"),
        "summary": escalation.get("summary"),
        "reason": escalation.get("reason"),
        "comment_url": escalation.get("comment_url"),
        "branch": escalation.get("branch"),
        "pr_url": escalation.get("pr_url"),
        "downgraded": bool(escalation.get("downgraded")),
        "options": [
            {
                "key": option.get("key"),
                "label": option.get("label"),
                "effect": option.get("effect"),
                # The detail is what the effect will do, not free text the
                # page renders: the counts are enough for a person to see the
                # shape of a split without carrying every child body to the
                # browser.
                "children": len((option.get("detail") or {}).get("children") or []),
                "closes": list((option.get("detail") or {}).get("closes") or []),
                "in_favour_of": (option.get("detail") or {}).get("in_favour_of"),
            }
            for option in escalation.get("options") or []
        ],
        "chat": [
            {
                "note": entry.get("note"),
                "asked_at": entry.get("asked_at"),
                # Whether the lane actually took the re-brief. A question
                # posted on the issue with nothing scheduled to answer it
                # looks identical to one that was, so the card says which.
                "requeued": entry.get("requeued"),
                "blocked_by": entry.get("blocked_by"),
            }
            for entry in escalation.get("chat") or []
        ],
        # The fixed way out, offered only while there is something to escape
        # from. Kept in its own list so the brief's options keep the numbered
        # keys 1 to 4 however many escapes there are.
        "escape": (
            [
                {
                    "key": option["key"],
                    "label": option["label"],
                    "effect": option["effect"],
                }
                for option in ESCAPE_OPTIONS
            ]
            if resolved is None
            else []
        ),
        "resolved": resolved,
        "open": resolved is None,
        # A brief running on this issue is a decision the server will refuse,
        # so the page reads this rather than offering buttons that 409.
        "briefing": receipt.get("state") in _ACTIVE,
        "work_item_id": receipt.get("work_item_id"),
    }


def escalations(receipts: list[dict]) -> list[dict]:
    """Every escalation on the board, newest issue first, open ones first."""
    views = [
        view for view in (escalation_view(receipt) for receipt in receipts) if view
    ]
    return sorted(
        views,
        key=lambda view: (not view["open"], -(view["issue_number"] or 0)),
    )


def intake_state(policy: dict, *, session: Session | None = None) -> dict:
    """What the board shows: the block, the last two audits, today's usage.

    This lives beside status rather than beside the intake loop because the
    board reads it, and the board must not link the reconciler to render a
    policy.
    """
    block = intake_policy(policy)
    cutoff = _now() - timedelta(hours=24)
    with _read_session(session) as db:
        # Delivery admissions, which is what the cap beside it bounds. The
        # board would otherwise show a fraction whose two halves counted
        # different things.
        admitted_today = delivery_admissions(db, cutoff)

        def latest(action: str) -> dict | None:
            row = db.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id.desc())
            ).first()
            if row is None:
                return None
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return {
                "created_at": created.isoformat(),
                "detail": json.loads(row.detail_json),
            }

        return {
            "policy": block,
            "admitted_today": admitted_today,
            "max_per_day": block["max_per_day"],
            "last_admitted": latest("intake_admitted"),
            "last_idle": latest("intake_idle"),
        }


_PROBLEM_ISSUE_ACTIONS = (
    "problem_issue_policy_observed",
    "problem_issue_scan_capped",
    "problem_issue_issue_scan_capped",
    "problem_issue_discovery_failed",
    "problem_issue_daily_capped",
    "problem_issue_write_started",
    "problem_issue_created",
    "problem_issue_write_refused",
    "problem_issue_source_refused",
    "problem_issue_write_uncertain",
    "problem_issue_reconcile_retry",
    "problem_issue_reconciled",
    "problem_issue_unresolved",
)


def problem_issues_state(policy: dict, *, session: Session | None = None) -> dict:
    """Producer policy and recent durable activity for the factory board."""
    block = problem_issues_policy(policy)
    cutoff = _now() - timedelta(hours=24)
    with _read_session(session) as db:
        writes = len(
            db.exec(
                select(FactoryAudit.id).where(
                    FactoryAudit.action == "problem_issue_write_started",
                    FactoryAudit.created_at >= cutoff,
                )
            ).all()
        )
        last = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action.in_(_PROBLEM_ISSUE_ACTIONS))
            .order_by(FactoryAudit.id.desc())
        ).first()
        last_event = None
        if last is not None:
            created = last.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            last_event = {
                "action": last.action,
                "created_at": created.isoformat(),
                "detail": json.loads(last.detail_json),
            }
        enabled_sources = sorted(
            source for source, enabled in block["sources"].items() if enabled
        )
        return {
            "policy": block,
            "status": ("on" if block["enabled"] and enabled_sources else "off"),
            "enabled_sources": enabled_sources,
            "writes_started_today": writes,
            "max_per_24_hours": block["max_per_24_hours"],
            "last_event": last_event,
        }


# Transitions the reconciler records, newest first, when review routing
# changes. They live here, beside the ledger, because the board reads them and
# the board must not link the module that reaches the token broker.
_VERDICT_ACTIONS = ("reviewer_fallback", "reviewer_restored", "review_waiting")


def latest_verdict(session: Session | None = None):
    """The last recorded routing decision, or None before the first tick."""
    with _read_session(session) as db:
        return db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action.in_(_VERDICT_ACTIONS))
            .order_by(FactoryAudit.id.desc())
        ).first()


def window_high(*, session: Session | None = None) -> bool:
    """Whether the 7-day window is currently counted as nearly spent.

    Read from the last recorded decision rather than from a fresh observation,
    so every node dispatched in one tick is routed by the same reading, and a
    replica restart does not forget a fallback mid-task.
    """
    row = latest_verdict(session)
    if row is None:
        return False
    try:
        detail = json.loads(row.detail_json)
        reset = detail.get("resets_at")
        if isinstance(reset, str):
            try:
                reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if reset_at.tzinfo is not None and reset_at <= _now():
                    return False
            except ValueError:
                pass
        return bool(detail.get("window_high", False))
    except (TypeError, ValueError):
        return False


def review_routing_view(policy: dict, *, session: Session | None = None) -> dict:
    """What the board shows about reviewer routing, read from the ledger.

    The reconciler observes the window and writes the decision; this reads it.
    A board render must not be able to make the page wait on a token broker.
    """
    block = quota_guard_policy(policy)
    with _read_session(session) as db:
        verdict = latest_verdict(db)
        last = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action.in_(REVIEWER_ROUTING_ACTIONS))
            .order_by(FactoryAudit.id.desc())
        ).first()
    try:
        detail = json.loads(verdict.detail_json) if verdict is not None else {}
    except (TypeError, ValueError):
        detail = {}
    seen = None
    if last is not None:
        created = last.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        seen = created.isoformat()
    # A routing decision can outlive its observation. Never present its old
    # percentage as current after observation loss or a broker/window expiry.
    used = detail.get("used_percent")
    quota_status = "unavailable"
    if verdict is not None and isinstance(used, (int, float)):
        observed = verdict.created_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = detail.get("observation_age_seconds") or 0
        age = age if isinstance(age, (int, float)) and math.isfinite(age) else 0
        fresh = (_now() - observed).total_seconds() + age <= QUOTA_GUARD_MAX_AGE_SECONDS
        unknown = last is not None and last.action == "quota_guard_unknown"
        quota_status = "unavailable" if unknown else "fresh" if fresh else "stale"
        reset = detail.get("resets_at")
        if isinstance(reset, str):
            try:
                reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if reset_at.tzinfo is not None and reset_at <= _now():
                    quota_status = "expired"
            except ValueError:
                pass
    return {
        "action": verdict.action if verdict is not None else None,
        "window_high": bool(detail.get("window_high", False)),
        "model": detail.get("model"),
        "used_percent": used if quota_status == "fresh" else None,
        "quota_status": quota_status,
        "pause_percent": block["claude_7d_pause_percent"],
        "resume_percent": block["claude_7d_resume_percent"],
        "last_action": last.action if last is not None else None,
        "last_seen_at": seen,
    }


def status(*, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        if control is None:
            return {
                "ok": False,
                "reason": "not_initialized",
                "state": "disabled",
                "receipts": [],
            }
        rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        receipts = [_snapshot(db, r) for r in rows]
        policy = json.loads(control.policy_json)
        return {
            "ok": True,
            "state": control.state,
            "policy": policy,
            "intake": intake_state(policy, session=db),
            "problem_issues": problem_issues_state(policy, session=db),
            "lanes": lane_usage(policy, receipts),
            "review_routing": review_routing_view(policy, session=db),
            "admitted_count": control.admitted_count,
            "version": control.version,
            "actor": control.actor,
            "receipts": receipts,
            "active_tasks": [r for r in receipts if r["state"] in _ACTIVE],
        }


def operator_direction(task_id: str, *, session: Session | None = None) -> dict | None:
    """The operator's answer that re-admitted this task, or None.

    Read by the planner prompt on the task's first round. It lives on the
    receipt rather than in the task text because the task text is the issue
    body captured at admission, and rewriting that would lose the one thing
    the planner needs to be able to tell apart: what the issue says, and what
    a person decided about it afterwards.
    """
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        if row is None or not row.direction_json:
            return None
        return json.loads(row.direction_json)


def task_snapshot(task_id: str, *, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        return (
            {"ok": False, "reason": "unknown_task"}
            if row is None
            else {"ok": True, **_snapshot(db, row, body=True)}
        )


def _generation_retirement_candidates(
    db: Session, current_generation: int
) -> list[FactoryReceipt]:
    """Old inert receipts and old unresolved cards, never live old work."""
    candidates = []
    rows = db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.generation < current_generation)
        .order_by(FactoryReceipt.id)
    ).all()
    for row in rows:
        if row.state in _ACTIVE:
            continue
        escalation = json.loads(row.escalation_json) if row.escalation_json else None
        unresolved = isinstance(escalation, dict) and escalation.get("resolved") is None
        if row.state in ("queued", ESCALATED) or unresolved:
            candidates.append(row)
    return candidates


def _retire_generation_candidate(
    db: Session,
    row: FactoryReceipt,
    current_generation: int,
    actor: str,
) -> dict:
    """Settle one old queue or card without moving it into the new policy."""
    previous_state = row.state
    escalation = json.loads(row.escalation_json) if row.escalation_json else None
    card_resolved = isinstance(escalation, dict) and escalation.get("resolved") is None
    receipt_retired = row.state in ("queued", ESCALATED)
    note = (
        f"Policy generation advanced past this receipt from {row.generation} to "
        f"{current_generation}; the old-generation work was retired without "
        "being re-stamped or resumed under the new policy."
    )
    if card_resolved:
        identity = decision_identity(
            {
                "id": row.id,
                "repo": row.repo,
                "generation": row.generation,
                "escalation": escalation,
            }
        )
        escalation["resolved"] = {
            "option_key": "escape:dismiss",
            "label": "Dismiss the escalation",
            "effect": "escape-dismiss",
            "actor": GENERATION_RETIREMENT_ACTOR,
            "note": note,
            "effects": {
                "dismissed": True,
                "receipt_retired": receipt_retired,
            },
            "decided_at": _now().isoformat(),
            "decision_id": identity,
        }
        row.escalation_json = _json(escalation)
    if receipt_retired:
        row.state = "cancelled"
    row.updated_at = _now()
    db.add(row)
    _audit(
        db,
        GENERATION_RETIREMENT_ACTOR,
        "generation_stale_receipt_retired",
        task_id=row.task_id,
        receipt_id=row.id,
        issue_number=row.issue_number,
        receipt_generation=row.generation,
        policy_generation=current_generation,
        previous_state=previous_state,
        state=row.state,
        receipt_retired=receipt_retired,
        escalation_resolved=card_resolved,
        configured_by=actor,
        reason=note,
    )
    return {"receipt_retired": receipt_retired, "card_resolved": card_resolved}


def reconcile_generation_stale_receipts(
    actor: str = GENERATION_RETIREMENT_ACTOR,
    *,
    limit: int = GENERATION_RECONCILIATION_LIMIT,
    session: Session | None = None,
) -> dict[str, int]:
    """Bounded repair for receipts stranded before configure gained retirement."""
    if type(limit) is not int or not 1 <= limit <= GENERATION_RECONCILIATION_LIMIT:
        raise ValueError("invalid generation reconciliation limit")
    from factory.orchestration.factory_decisions import decision_in_flight

    counts = {"candidates": 0, "retired": 0, "cards_resolved": 0, "blocked": 0}
    with _locked_session(session) as (db, control):
        policy = json.loads(control.policy_json or "{}")
        current_generation = policy.get("generation")
        if type(current_generation) is not int:
            return counts
        reconciled = 0
        for row in _generation_retirement_candidates(db, current_generation):
            if reconciled >= limit:
                break
            counts["candidates"] += 1
            if decision_in_flight(db, row.id):
                counts["blocked"] += 1
                continue
            result = _retire_generation_candidate(db, row, current_generation, actor)
            reconciled += 1
            counts["retired"] += int(result["receipt_retired"])
            counts["cards_resolved"] += int(result["card_resolved"])
    return counts


def set_control(
    action: str,
    actor: str,
    *,
    policy: dict | None = None,
    task_id: str | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if action not in (
        "configure",
        "enable",
        "pause_admissions",
        "pause_task",
        "resume_task",
        "stop",
    ):
        raise ValueError("invalid control action")
    configured = validate_policy(policy) if action == "configure" else None
    if action != "configure" and policy is not None:
        raise ValueError("policy requires configure action")
    if (action in ("pause_task", "resume_task")) != (task_id is not None):
        raise ValueError("task_id is required only for task pause/resume")
    if action == "resume_task" and session is None:
        # Resuming an escalation is answering it. There is no node to unpause:
        # the task settled and left the lane, so the only way back in is the
        # decision path, and the recommendation is what the operator gets for
        # pressing resume rather than reading the card. Skipped when a caller
        # supplied a session, because applying an option writes to GitHub and
        # must never run inside somebody else's open transaction.
        with _read_session() as db:
            row = _receipt(db, task_id)
            escalated = row is not None and row.state == ESCALATED
        if escalated:
            from factory.orchestration.factory_decisions import resume_escalated

            return resume_escalated(task_id, actor)
    with _locked_session(session) as (db, control):
        reason = None
        configure_detail = {}
        if control.state == "stopped" and action != "stop":
            reason = "stopped"
        elif action == "configure":
            active = db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).all()
            previous = json.loads(control.policy_json or "{}")
            configure_detail["active_tasks_on_previous_policy"] = len(active)
            # An identical retry is harmless. A changed policy must advance
            # generation while work is running, keeping the old queue inert.
            if (
                active
                and configured != _policy_for_generation_comparison(previous)
                and configured["generation"] <= previous.get("generation", -1)
            ):
                reason = "generation_not_advanced"
            else:
                candidates = (
                    _generation_retirement_candidates(db, configured["generation"])
                    if configured["generation"] > previous.get("generation", -1)
                    else []
                )
                from factory.orchestration.factory_decisions import (
                    decision_in_flight,
                )

                blocked = [
                    row.id for row in candidates if decision_in_flight(db, row.id)
                ]
                if blocked:
                    reason = "generation_retirement_decision_in_flight"
                    configure_detail["blocked_receipt_ids"] = blocked
                else:
                    retired = [
                        _retire_generation_candidate(
                            db, row, configured["generation"], actor
                        )
                        for row in candidates
                    ]
                    configure_detail.update(
                        generation_receipts_retired=sum(
                            int(item["receipt_retired"]) for item in retired
                        ),
                        generation_escalations_resolved=sum(
                            int(item["card_resolved"]) for item in retired
                        ),
                    )
                    control.policy_json = _json(configured)
                    # Configuration does not change execution authority: enabled
                    # work continues, paused admissions stay paused, and initial
                    # configuration remains disabled until an explicit enable.
        elif action == "enable":
            if not json.loads(control.policy_json):
                reason = "not_configured"
            else:
                control.state = "enabled"
        elif action == "pause_admissions":
            control.state = "paused"
        elif action in ("pause_task", "resume_task"):
            row = _receipt(db, task_id)
            if row is None or row.state not in _ACTIVE:
                reason = "task_not_active"
            elif row.cancellation_requested:
                reason = "cancellation_pending"
            else:
                row.task_paused = action == "pause_task"
                row.updated_at = _now()
                db.add(row)
        elif action == "stop":
            control.state = "stopped"
            control.stopped_at = control.stopped_at or _now()
            for row in db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).all():
                row.cancellation_requested = True
                row.updated_at = _now()
                db.add(row)
            # An escalated receipt runs nothing, so there is no cancellation to
            # reconcile and cancel_owned never reaches it: the reconciler only
            # visits active tasks. Settling it here is what stops a stop
            # leaving a card waiting on a decision for a lane that is shut.
            # The task keeps escalated as its own outcome, which is what
            # happened to it; the receipt records what the stop then decided.
            for row in db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state == ESCALATED)
            ).all():
                row.state, row.cancellation_requested = "cancelled", True
                row.updated_at = _now()
                db.add(row)
        if reason is None:
            control.version += 1
            control.updated_at = _now()
            control.actor = actor
            db.add(control)
        _audit(
            db,
            actor,
            action,
            task_id=task_id,
            ok=reason is None,
            reason=reason,
            policy=configured,
            **configure_detail,
        )
        return {
            "ok": reason is None,
            "reason": reason,
            "state": control.state,
            "version": control.version,
        }


def request_control(
    action: str,
    actor: str,
    *,
    request_key: str,
    expected_version: int,
    task_id: str | None = None,
) -> dict:
    """Apply a version-bound operator request once through the control owner.

    The control row serializes the request ledger and the existing mutation in
    one transaction. A replay returns its original acknowledgement, even if a
    newer command has since changed the factory. It never reapplies the action.
    Task resume only unpauses active work here: selecting an escalation's
    recommendation is a separate decision and needs its own exact identity.
    """
    if action not in (
        "enable",
        "pause_admissions",
        "pause_task",
        "resume_task",
        "stop",
    ):
        raise ValueError("unsupported requested control")
    request = {
        "action": action,
        "actor": _text(actor, "actor"),
        "request_key": _text(request_key, "request_key"),
        "expected_version": _integer(
            expected_version, "expected_version", 0, 2**63 - 1
        ),
        "task_id": _text(task_id, "task_id") if task_id is not None else None,
    }
    if (action in ("pause_task", "resume_task")) != (task_id is not None):
        raise ValueError("task_id is required only for task pause/resume")
    with _locked_session() as (db, control):
        previous = db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.action == "control_request",
                FactoryAudit.actor == actor,
            )
        ).all()
        for raw in previous:
            record = json.loads(raw)
            if record["request"]["request_key"] == request_key:
                if record["request"] != request:
                    return {"ok": False, "reason": "conflicting_control_request"}
                return record["result"]
        if control.version != expected_version:
            result = {
                "ok": False,
                "reason": "control_version_changed",
                "state": control.state,
                "version": control.version,
            }
        else:
            # Supplying the locked session deliberately uses the active-task
            # branch of set_control, never its automatic escalation decision.
            result = set_control(action, actor, task_id=task_id, session=db)
        result = {
            **result,
            "request_key": request_key,
            "task_id": task_id,
            "acknowledged_at": _now().isoformat(),
        }
        _audit(
            db,
            actor,
            "control_request",
            task_id=task_id,
            request=request,
            result=result,
        )
        return result


def _can_start(
    db: Session, control: FactoryControl, task_id: str, start_key: str | None = None
) -> dict:
    row = _receipt(db, task_id)
    reason = None
    if control.state == "stopped":
        reason = "stopped"
    elif control.state not in ("enabled", "paused"):
        reason = "disabled"
    elif row is None or row.state not in _ACTIVE:
        reason = "task_not_active"
    elif row.cancellation_requested:
        reason = "cancellation_pending"
    elif row.task_paused:
        reason = "task_paused"
    elif row.state == "uncertain" or any(
        s.status == "uncertain" for s in _starts(db, task_id)
    ):
        reason = "uncertain_outcome"
    elif row is not None:
        task = db.get(SwarmTask, task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        from factory.orchestration.factory_funding_limits import (
            pending,
            review_authority,
            amendment,
        )

        request = pending(db, task_id)
        oversight = review_authority(db, task_id, start_key)
        timeout = _effective_policy(db, row)["task_timeout_seconds"]
        matched = _START_KEY.match(start_key or "")
        funding_key = matched and matched.group("node_key").startswith(
            "conductor_funding_"
        )
        grant = amendment(db, task_id)
        already_reserved = any(
            s.start_key == start_key and s.status == "reserved"
            for s in _starts(db, task_id)
        )
        if funding_key and not oversight:
            reason = "funding_review_expired"
        elif request and not oversight:
            reason = "funding_review_pending"
        elif (
            grant
            and not oversight
            and not already_reserved
            and _now() >= datetime.fromisoformat(grant["review_due_at"])
        ):
            reason = "funding_lease_due"
        elif not oversight and _now() >= _recovery_deadline(
            db, task_id, admitted + timedelta(seconds=timeout)
        ):
            reason = "task_deadline"
    return {"ok": reason is None, "reason": reason}


def can_start(
    task_id: str, *, session: Session | None = None, start_key: str | None = None
) -> dict:
    """Fresh read fence. Use start_guard to serialize it with session creation."""
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        result = (
            {"ok": False, "reason": "not_initialized"}
            if control is None
            else _can_start(db, control, task_id, start_key)
        )

        from factory.orchestration.factory_funding_limits import pending, amendment

        funding = pending(db, task_id) or amendment(db, task_id)
        if funding:
            result["funding"] = funding
        if result["ok"]:
            grant = continuation_grant(task_id, session=db)
            if grant:
                result["continuation"] = grant
        return result


@contextmanager
def start_guard(
    task_id: str, *, session: Session | None = None, start_key: str | None = None
) -> Iterator[dict]:
    """Serialize stop with synchronous session persistence, never guest waits.

    Hold this guard only across the API that durably creates the session and
    pending turn. Once it returns, an admitted session remains subject to explicit
    cancellation/reconciliation. A supplied session must commit before any wait.
    """
    with _locked_session(session) as (db, control):
        token = _ACTIVE_START_SESSION.set(db)
        try:
            yield _can_start(db, control, task_id, start_key)
        finally:
            _ACTIVE_START_SESSION.reset(token)


def active_start_session() -> Session | None:
    """Return the transaction holding the current synchronous start fence."""
    return _ACTIVE_START_SESSION.get()


def authorize_start(
    task_id: str,
    start_key: str,
    actor: str,
    *,
    model: str,
    max_cost_usd: float,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    start_key = _text(start_key, "start_key", 256)
    model = _text(model, "model", 128)
    cost = _money(max_cost_usd, "max_cost_usd")
    with _locked_session(session) as (db, control):
        check = _can_start(db, control, task_id, start_key)
        if not check["ok"]:
            return check
        row = _receipt(db, task_id)
        policy = _effective_policy(db, row)
        starts = _starts(db, task_id)
        existing = next((s for s in starts if s.start_key == start_key), None)
        if existing:
            if existing.model != model or existing.max_cost_usd != cost:
                return {"ok": False, "reason": "conflicting_start_pin"}
            return {"ok": True, "replayed": True, "start": _start_dict(existing)}
        from factory.orchestration import factory_funding_limits as factory_funding

        oversight = factory_funding.review_authority(db, task_id, start_key)
        if oversight and (model != "astra" or cost != factory_funding.REVIEW_COST_USD):
            return {"ok": False, "reason": "funding_review_pin_mismatch"}
        if (
            factory_funding.enabled()
            or factory_funding.amendment(db, task_id)
            or oversight
        ):
            total = factory_funding.objective(db, task_id)
            if (
                total["committed_cost_usd"] + cost
                > factory_funding.OBJECTIVE_CEILING_USD
            ):
                return {"ok": False, "reason": "objective_budget_limit"}
        budget = _accounting(starts)
        planner = _planner_key(start_key)
        grant = continuation_grant(task_id, session=db)
        matched = _START_KEY.match(start_key)
        node_key = matched.group("node_key") if matched else None
        reason = None
        # One reserved start per parallel slot. At the default limit of one
        # this is the original single-flight fence, unchanged.
        if sum(s.status == "reserved" for s in starts) >= parallel_limit(policy):
            reason = "start_pending"
        elif (
            grant
            and not planner
            and (
                node_key not in grant["node_keys"]
                or _accounting([s for s in starts if _start_node_key(s) == node_key])[
                    "turns_used"
                ]
                >= 1
                or budget["turns_used"] >= grant["work_turn_ceiling"]
            )
        ):
            reason = "continuation_scope_exhausted"
        elif model not in policy["allowed_models"]:
            reason = "model_not_allowed"
        # A planning round is cheap enough that the task budget alone would
        # admit hundreds of them, and a refused decision can mint the next
        # planner every tick, so deliberation is bounded on its own count.
        elif (
            not oversight
            and planner
            and budget["planner_turns_used"] >= planner_turn_cap(policy)
        ):
            reason = "planner_turn_limit"
        # The plan sizes the work, so the bound is the derived allowance rather
        # than a policy number. Nothing but an accepted plan edit moves it.
        elif (
            not planner
            and budget["turns_used"] >= _stored_allowance(row, policy)["turns"]
        ):
            reason = "turn_limit"
        # Both kinds of start still answer to the one task budget.
        elif not oversight and (
            budget["committed_cost_usd"] + cost > policy["task_budget_usd"]
            or (
                cost > policy["turn_budget_usd"]
                and not any(
                    run.dispatch_key == start_key
                    and run.status == "admitted"
                    and run.reserved_cost_usd == cost
                    and json.loads(run.pin_json or "{}").get("model") == model
                    for run in db.exec(
                        select(SwarmNodeRun).where(SwarmNodeRun.task_id == task_id)
                    ).all()
                )
            )
        ):
            reason = "budget_limit"
        if reason:
            _audit(
                db,
                actor,
                "authorize_start",
                task_id=task_id,
                ok=False,
                reason=reason,
                start_key=start_key,
            )
            return {"ok": False, "reason": reason}
        start = FactoryStart(
            task_id=task_id,
            start_key=start_key,
            actor=actor,
            model=model,
            max_cost_usd=cost,
        )
        db.add(start)
        db.flush()
        _audit(
            db, actor, "authorize_start", task_id=task_id, ok=True, start_key=start_key
        )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def record_start_outcome(
    task_id: str,
    start_key: str,
    status: str,
    actor: str,
    *,
    cost_usd: float | None = None,
    accounting_basis: str | None = None,
    session_id: int | None = None,
    reconciled: bool = False,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if type(reconciled) is not bool:
        raise ValueError("invalid reconciled flag")
    if status not in (*_TERMINAL, "uncertain", "escalated"):
        raise ValueError("invalid start outcome")
    if accounting_basis is not None and accounting_basis not in FREE_START_BASES:
        raise ValueError("invalid accounting basis")
    cost = None if cost_usd is None else _money(cost_usd, "cost_usd", zero=True)
    if session_id is not None:
        _integer(session_id, "session_id", 1, 2**31 - 1)
    # Known completion without provider cost consumes the reserved ceiling.
    # Only uncertain execution retains an active reservation.
    # Escalation is terminal work without delivery. Keep its richer graph
    # outcome while settling this reservation under the existing failed status.
    effective = "failed" if status == "escalated" else status
    with _locked_session(session) as (db, _control):
        start = next(
            (s for s in _starts(db, task_id) if s.start_key == start_key), None
        )
        if start is None:
            return {"ok": False, "reason": "unknown_start"}
        if (
            start.status == effective
            and start.cost_usd == cost
            and start.session_id == session_id
        ):
            # A settlement recorded before this evidence existed carries no
            # basis. Backfill it rather than refusing the replay: it is the
            # same settlement, and a start left at its ceiling holds the turn
            # and the budget the graph has already released (#6045). A basis
            # already on the row is never overwritten.
            if accounting_basis is not None and start.accounting_basis is None:
                start.accounting_basis = accounting_basis
                start.updated_at = _now()
                db.add(start)
            return {"ok": True, "replayed": True, "start": _start_dict(start)}
        if start.status in _TERMINAL:
            return {"ok": False, "reason": "conflicting_outcome"}
        if start.status == "uncertain" and not reconciled:
            return {"ok": False, "reason": "reconciliation_required"}
        if start.session_id is not None and session_id != start.session_id:
            return {"ok": False, "reason": "conflicting_session"}
        start.status, start.cost_usd, start.session_id = effective, cost, session_id
        start.accounting_basis = accounting_basis
        start.updated_at = _now()
        db.add(start)
        row = _receipt(db, task_id)
        if effective == "uncertain":
            row.state = "uncertain"
        elif row.state == "uncertain" and not any(
            s.status == "uncertain" and s.id != start.id for s in _starts(db, task_id)
        ):
            row.state = "admitted"
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            actor,
            "record_start_outcome",
            task_id=task_id,
            start_key=start_key,
            status=effective,
            reconciled=reconciled,
        )
        if effective in _TERMINAL and cost is not None and cost > start.max_cost_usd:
            _audit(
                db,
                actor,
                "cost_over_reservation",
                task_id=task_id,
                start_key=start_key,
                workflow_id=start_key,
                status=status,
                cost_usd=cost,
                reserved_cost_usd=start.max_cost_usd,
                overage_usd=cost - start.max_cost_usd,
            )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def landing_recovery_barrier(task_id: str, *, session=None) -> dict | None:
    """Runs at or below this durable boundary predate the latest recovery."""
    with _read_session(session) as db:
        event = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "landing_recovery_requested",
            )
            .order_by(FactoryAudit.id.desc())
            .limit(1)
        ).first()
        if event is None:
            return None
        detail = json.loads(event.detail_json)
        floor = detail.get("run_id_floor")
        if floor is None:
            # Requests from the first rollout predate the explicit run fence.
            floor = (
                db.exec(
                    select(SwarmNodeRun.id)
                    .where(
                        SwarmNodeRun.task_id == task_id,
                        SwarmNodeRun.created_at <= event.created_at,
                    )
                    .order_by(SwarmNodeRun.id.desc())
                    .limit(1)
                ).first()
                or 0
            )
        rounds = db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "landing_recovery_round",
            )
        ).all()
        return {
            "request_id": event.id,
            "run_id_floor": floor,
            "pr_number": detail.get("pr_number"),
            "head_sha": detail.get("head_sha"),
            "round_recorded": any(
                json.loads(raw).get("request_id") == event.id for raw in rounds
            ),
        }


def finish_task(
    task_id: str,
    outcome: str,
    actor: str,
    *,
    evidence: dict | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if outcome not in (*_SETTLED, "uncertain"):
        raise ValueError("invalid task outcome")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - {
            "pr_url",
            "head_sha",
            "review_session_id",
            # Which model approved. A spent Claude window routes review down
            # the reviewer pool, so an accepted delivery has to say who gave
            # the approval rather than leaving it inferred from the date.
            "reviewer_model",
            "state",
            "reason",
        }:
            raise ValueError("unsupported task evidence")
        for key, value in evidence.items():
            if key == "review_session_id":
                _integer(value, key, 1, 2**31 - 1)
            else:
                _text(value, key, 1024)
        if len(_json(evidence)) > 4096:
            raise ValueError("task evidence too large")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        if row.state in _SETTLED:
            last = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "finish_task",
                )
                .order_by(FactoryAudit.id.desc())
            ).first()
            same = (
                row.state == outcome
                and last is not None
                and json.loads(last.detail_json).get("evidence") == evidence
            )
            return {"ok": same, "reason": None if same else "conflicting_outcome"}
        if outcome == "succeeded":
            barrier = landing_recovery_barrier(task_id, session=db)
            if barrier is not None:
                review = (
                    db.exec(
                        select(SwarmNodeRun).where(
                            SwarmNodeRun.task_id == task_id,
                            SwarmNodeRun.id > barrier["run_id_floor"],
                            SwarmNodeRun.node_key.startswith("review_"),
                            SwarmNodeRun.status == "succeeded",
                            SwarmNodeRun.session_id
                            == (evidence or {}).get("review_session_id"),
                        )
                    ).first()
                    if (evidence or {}).get("review_session_id")
                    else None
                )
                try:
                    outcome_body = (
                        json.loads(review.outcome_json or "{}") if review else {}
                    )
                except (TypeError, ValueError):
                    outcome_body = {}
                artifact = (
                    outcome_body.get("value") or outcome_body.get("artifact") or {}
                    if isinstance(outcome_body, dict)
                    else {}
                )
                exact_head = (evidence or {}).get("head_sha")
                approval_matches = bool(
                    review is not None
                    and artifact.get("verdict") == "approve"
                    and artifact.get("pr_number") == barrier["pr_number"]
                    and artifact.get("head_sha") == exact_head
                    and review.head_sha == exact_head
                )
                implementer_sessions: set[int] = set()
                if approval_matches:
                    candidates = db.exec(
                        select(SwarmNodeRun).where(
                            SwarmNodeRun.task_id == task_id,
                            SwarmNodeRun.status == "succeeded",
                        )
                    ).all()
                    for candidate in candidates:
                        if not candidate.node_key.startswith(
                            ("implement_", "integrate_", "correct_")
                        ):
                            continue
                        try:
                            body = json.loads(candidate.outcome_json or "{}")
                        except (TypeError, ValueError):
                            continue
                        value = (
                            body.get("value") or body.get("artifact") or {}
                            if isinstance(body, dict)
                            else {}
                        )
                        if (
                            value.get("pr_number") == barrier["pr_number"]
                            and value.get("head_sha") == exact_head
                            and candidate.head_sha == exact_head
                            and candidate.id > barrier["run_id_floor"]
                            and candidate.id < review.id
                            and candidate.session_id is not None
                        ):
                            implementer_sessions.add(candidate.session_id)
                independent = bool(
                    review is not None
                    and review.session_id is not None
                    and implementer_sessions
                    and review.session_id not in implementer_sessions
                )
                if (
                    not barrier["round_recorded"]
                    or not approval_matches
                    or not independent
                ):
                    return {"ok": False, "reason": "landing_recovery_pending"}
        if (
            outcome != "uncertain"
            and _accounting(_starts(db, task_id))["unresolved_starts"]
        ):
            return {"ok": False, "reason": "unresolved_starts"}
        row.state = outcome
        row.updated_at = _now()
        db.add(row)
        if outcome in _SETTLED:
            # An escalated task is settled the same way a terminal one is.
            # Nothing the server does on its own runs another node on it, and
            # a decision that re-admits the work mints a new task rather than
            # waking this one.
            task = db.get(SwarmTask, task_id)
            task.settled_at = _now()
            task.start_state = outcome
            db.add(task)
        _audit(
            db,
            actor,
            "finish_task",
            task_id=task_id,
            outcome=outcome,
            evidence=evidence,
        )
        return {"ok": True, "state": outcome}


def request_landing_recovery(
    task_id: str,
    pr_number: int,
    head_sha: str,
    source: str,
    actor: str,
    *,
    session: Session | None = None,
    reason: str = "merge_conflict",
) -> dict:
    """Reopen one delivery for bounded assessment without resetting its spend.

    One durable request is allowed per settlement, including unchanged-head
    retries. The conductor can also request recovery before first settlement.
    A request keeps its identity across retries and cannot extend its deadline.
    """
    actor = _text(actor, "actor")
    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("invalid pr_number")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("invalid head_sha")
    if source not in ("delivered_pr", "merge_queue"):
        raise ValueError("invalid landing recovery source")
    if reason not in ("merge_conflict", "queue_ejection"):
        raise ValueError("invalid landing recovery reason")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        events = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action.in_(("landing_recovery_requested", "finish_task")),
            )
            .order_by(FactoryAudit.id)
        ).all()
        previous = [
            event for event in events if event.action == "landing_recovery_requested"
        ]
        latest_finish = max(
            (event.id for event in events if event.action == "finish_task"), default=0
        )
        barrier = landing_recovery_barrier(task_id, session=db) if previous else None
        replaying = bool(
            previous
            and (previous[-1].id > latest_finish or not barrier["round_recorded"])
        )
        if replaying and row.state == "admitted":
            return {"ok": True, "replayed": True, "state": row.state}
        if not replaying and len(previous) >= MAX_LANDING_RECOVERIES:
            return {"ok": False, "reason": "recovery_limit"}
        if row.state not in ("admitted", "succeeded"):
            return {"ok": False, "reason": "task_not_correctable"}
        if row.task_paused or row.cancellation_requested:
            return {"ok": False, "reason": "task_paused"}
        if any(
            start.status in ("reserved", "uncertain") for start in _starts(db, task_id)
        ):
            return {"ok": False, "reason": "unresolved_execution"}
        if row.state == "succeeded":
            live_policy = json.loads(_control.policy_json or "{}")
            if _control.state != "enabled" or not auto_merge_enabled(live_policy):
                return {"ok": False, "reason": "factory_disabled"}
            active = db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).all()
            from factory.orchestration.factory_models import same_work

            if any(same_work(r, row) and r.task_id != task_id for r in active):
                return {"ok": False, "reason": "issue_already_active"}
            if (
                sum(
                    (r.routing_tier or lane_for(receipt_task_class(r))) == "delivery"
                    for r in active
                )
                >= lane_limits(live_policy)["delivery"]
            ):
                return {"ok": False, "reason": "delivery_capacity"}
            row.state = "admitted"
            row.updated_at = _now()
            db.add(row)
            task = db.get(SwarmTask, task_id)
            task.settled_at = None
            task.start_state = "factory"
            task.start_updated_at = _now()
            db.add(task)
        if replaying:
            return {"ok": True, "replayed": True, "state": row.state}
        run_floor = (
            db.exec(
                select(SwarmNodeRun.id)
                .where(
                    SwarmNodeRun.task_id == task_id,
                )
                .order_by(SwarmNodeRun.id.desc())
                .limit(1)
            ).first()
            or 0
        )
        _audit(
            db,
            actor,
            "landing_recovery_requested",
            task_id=task_id,
            run_id_floor=run_floor,
            pr_number=pr_number,
            head_sha=head_sha,
            source=source,
            reason=reason,
            deadline_at=(
                _now() + timedelta(seconds=LANDING_RECOVERY_TIMEOUT_SECONDS)
            ).isoformat(),
        )
        return {"ok": True, "replayed": False, "state": row.state}


_SESSIONLESS_START_TERMINAL_WORKFLOW_STATUSES = frozenset({"CANCELLED", "ERROR"})


def reconcile_sessionless_start(
    task_id: str,
    node_key: str,
    attempt: int,
    actor: str,
    *,
    workflow_status: str | None,
    session: Session | None = None,
) -> dict:
    """Atomically fail one aged start proven never to have made a session.

    The factory control lock is the same fence held by ``start_guard`` while a
    node persists its session and prompt. The proof then locks the capacity
    pool, run, start, deterministic session identity, permit, and receipt
    evidence. A delayed creator therefore finishes before this read or finds a
    terminal run after this commit; it cannot cross the settlement.

    Refusals are ordinary observations for the periodic sweeper. Unexpected
    lookup failures raise and roll the whole transaction back, so unavailable
    evidence can never become a no-session proof. The caller must also supply
    the exact owning DBOS workflow's terminal error or cancellation status as
    external cessation evidence.
    """
    from factory.execution.api import inspect_lost_before_session_factory_attempt
    from factory.orchestration import graph

    actor = _text(actor, "actor")
    node_key = _text(node_key, "node_key")
    _integer(attempt, "attempt", 1, 2**31 - 1)
    if workflow_status not in _SESSIONLESS_START_TERMINAL_WORKFLOW_STATUSES:
        return {"ok": False, "reason": "workflow_not_terminal"}
    with _locked_session(session) as (db, _control):
        run = db.exec(
            select(SwarmNodeRun)
            .where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt == attempt,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if run is None:
            return {"ok": False, "reason": "unknown_attempt"}
        if run.status != "admitted":
            return {"ok": False, "reason": "attempt_not_admitted"}
        try:
            pin = json.loads(run.pin_json or "null")
        except (TypeError, ValueError):
            pin = None
        if not isinstance(pin, dict) or pin.get("workflow_id") != run.dispatch_key:
            return {"ok": False, "reason": "missing_attempt_pin"}
        proof, refusal = inspect_lost_before_session_factory_attempt(db, pin)
        if proof is None:
            return {"ok": False, "reason": refusal}
        proof = {**proof, "workflow_status": workflow_status}

        # The start ledger is ordered first, matching task settlement's
        # unresolved-start constraint. Both writes still share this transaction,
        # so any graph refusal rolls the start back to reserved.
        charged = record_start_outcome(
            task_id,
            run.dispatch_key,
            "failed",
            actor,
            cost_usd=0.0,
            accounting_basis="no_model_post",
            session_id=None,
            reconciled=True,
            session=db,
        )
        if not charged["ok"]:
            raise ValueError(f"start_outcome_refused: {charged['reason']}")
        result = {
            "status": "failed",
            "session_id": None,
            "attempt": attempt,
            "cost_usd": 0.0,
            "cost_basis": "unknown",
            "accounting": "unknown_cost",
            "head_sha": run.head_sha,
            "reason": "never_dispatched",
            "previous_outcome": json.loads(run.outcome_json or "{}"),
            "never_dispatched": proof,
        }
        settled = graph.record_outcome(
            task_id,
            node_key,
            attempt,
            "failed",
            0.0,
            run.head_sha,
            _json(result),
            session=db,
        )
        if not settled.ok:
            raise ValueError(f"outcome_refused: {settled.refusal_code}")
        _audit(
            db,
            actor,
            "sessionless_start_settled",
            task_id=task_id,
            workflow_id=run.dispatch_key,
            node_key=node_key,
            attempt=attempt,
            reason="never_dispatched",
            identity=proof,
        )
        return {"ok": True, "session_id": None, "outcome": result}


def settle_lost_attempt(
    task_id: str,
    node_key: str,
    attempt: int,
    actor: str,
    *,
    session: Session | None = None,
) -> dict:
    """Release one attempt lost before its session or guest was created.

    The operator repair for #6025, called in process from a backend pod the way
    finish_task is. Confirm the node's DBOS workflow is terminal before calling
    this: the reconciler only reaches the same settlement after the workflow
    status is terminal, and this path has no DBOS handle to check it, so an
    attempt settled while its workflow is still alive on another replica would
    run on with nothing recording or cancelling it. Today the only alternative is cancelling the whole receipt
    and re-admitting the task under a new generation, which throws away every
    other attempt on it.

    This applies one of the exact proofs in execution/reconciliation.py and
    refuses with a ValueError naming the first failed condition when the
    attempt is any other shape, so a live start, bound guest, captured receipt
    or settled permit is never settled by hand here. Deliberately not gated on
    FACTORY_LOST_BEFORE_GUEST_SETTLEMENT_ENABLED: while that flag is off this
    is the repair, and it is a human action either way. There is no HTTP route.
    """
    from factory.execution.api import (
        inspect_lost_before_guest_factory_attempt,
        inspect_lost_before_session_factory_attempt,
        settle_lost_before_guest_factory_attempt,
        settle_lost_before_session_factory_attempt,
    )
    from factory.orchestration import graph
    from factory.orchestration.models import SwarmNodeRun

    actor = _text(actor, "actor")
    node_key = _text(node_key, "node_key")
    _integer(attempt, "attempt", 1, 2**31 - 1)
    with _locked_session(session) as (db, _control):
        run = db.exec(
            select(SwarmNodeRun)
            .where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt == attempt,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if run is None:
            raise ValueError("unknown_attempt")
        if run.status not in ("admitted", "dispatched", "uncertain"):
            raise ValueError("attempt_not_active")
        if run.cost_usd is not None:
            raise ValueError("attempt_already_priced")
        if db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt > attempt,
            )
        ).first():
            raise ValueError("newer_attempt_exists")
        try:
            pin = json.loads(run.pin_json or "null")
        except (TypeError, ValueError):
            pin = None
        if not isinstance(pin, dict) or pin.get("workflow_id") != run.dispatch_key:
            raise ValueError("missing_attempt_pin")
        session_id = run.session_id
        if session_id is None:
            # Legacy rows and cached pre-binding step outputs can still be
            # session-less. Resolve only the exact deterministic owner.
            from factory.orchestration.node_workflows import resolve_node_session_id

            session_id = resolve_node_session_id(pin, session=db)
        if session_id is None:
            proof, refusal = inspect_lost_before_session_factory_attempt(db, pin)
            if proof is None:
                raise ValueError(refusal)
            settle_lost_before_session_factory_attempt(db, pin, proof)
            lost_phase = "lost_before_session"
        else:
            proof, refusal = inspect_lost_before_guest_factory_attempt(
                db, pin, session_id
            )
            if proof is None:
                raise ValueError(refusal)
            settle_lost_before_guest_factory_attempt(db, pin, proof)
            lost_phase = "lost_before_guest"
        reason = (
            "lost_before_session: operator settled an attempt that never created a session"
            if lost_phase == "lost_before_session"
            else "lost_before_guest: operator settled an attempt whose guest was never bound"
        )
        result = {
            "status": "failed",
            "session_id": proof["session_id"],
            "attempt": attempt,
            "cost_usd": 0.0,
            "cost_basis": "unknown",
            "accounting": "unknown_cost",
            "head_sha": run.head_sha,
            "reason": reason,
            "previous_outcome": json.loads(run.outcome_json or "{}"),
            lost_phase: proof,
        }
        if run.session_id is None and proof["session_id"] is not None:
            bound = graph.record_dispatch(
                task_id,
                node_key,
                attempt,
                proof["session_id"],
                run.base_sha,
                session=db,
            )
            if not bound.ok:
                raise ValueError(f"dispatch_refused: {bound.refusal_code}")
        settled = graph.record_outcome(
            task_id,
            node_key,
            attempt,
            "failed",
            0.0,
            run.head_sha,
            _json(result),
            session=db,
        )
        if not settled.ok:
            raise ValueError(f"outcome_refused: {settled.refusal_code}")
        charged = record_start_outcome(
            task_id,
            run.dispatch_key,
            "failed",
            actor,
            cost_usd=0.0,
            accounting_basis=(
                "no_model_post" if lost_phase == "lost_before_session" else None
            ),
            session_id=proof["session_id"],
            reconciled=True,
            session=db,
        )
        if not charged["ok"]:
            raise ValueError(f"start_outcome_refused: {charged['reason']}")
        _audit(
            db,
            actor,
            "stop_settled",
            task_id=task_id,
            workflow_id=run.dispatch_key,
            reason=lost_phase,
            session_id=proof["session_id"],
            identity=proof,
            cessation_confirmed=True,
            intervention_required=False,
        )
        return {"ok": True, "session_id": proof["session_id"], "outcome": result}
