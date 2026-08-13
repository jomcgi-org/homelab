"""Server-owned composition for the swarm run surfaces.

This module deliberately knows nothing about FastAPI.  DBOS objects are kept at
the edge and reduced to the v4 payload here, so the browser does not infer
workflow state or compose evidence-bearing prose.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from swarm import config
from swarm.rationale import parse_rationale
from swarm.deviations import compute_deviations

logger = logging.getLogger(__name__)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _status(workflow: Any) -> Any:
    getter = getattr(workflow, "get_status", None)
    return getter() if getter else workflow


def _attributes(workflow: Any, status: Any) -> dict:
    for obj in (status, workflow):
        attrs = _value(obj, "attributes")
        if isinstance(attrs, dict):
            return attrs
        getter = getattr(obj, "get_attributes", None)
        if getter:
            result = getter()
            if isinstance(result, dict):
                return result
    return {}


def _input(status: Any) -> dict:
    value = _value(status, "input", {})
    if not isinstance(value, (dict, list, tuple)) and value is not None:
        value = _value(value, "args", value)
    if isinstance(value, dict) and "args" in value:
        value = value["args"]
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        names = ("task", "repo", "branch", "budget_usd")
        return dict(zip(names, value))
    return {}


def _iso(value: Any) -> str | None:
    """Serialize a timestamp, or emit nothing at all.

    Any non-datetime used to pass through untouched, so a value the server
    could not serialize still reached the client and was parsed there. The run
    header timestamp comes from the DBOS workflow status while attempt
    timestamps come from session rows, and when the workflow side arrived as
    something else, `Date.parse` returned NaN and the client's `Number(x) || 0`
    laundered it into a confident "0s ago" printed beside a real elapsed time.

    A timestamp that cannot be serialized is absent, and absent has to look
    absent. Strings are trusted only if they parse as a timestamp.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return (
                datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    return None


def _children(dbos: Any, workflow_id: str) -> list[Any]:
    return list(
        dbos.list_workflows(parent_workflow_id=workflow_id, load_input=True) or []
    )


def _steps(dbos: Any, workflow_id: str) -> list[Any]:
    rows = dbos.list_workflow_steps(workflow_id) or []
    return [
        row
        for row in rows
        if "poll_turn" not in str(_value(row, "function_name", _value(row, "name", "")))
        and "DBOS.sleep"
        not in str(_value(row, "function_name", _value(row, "name", "")))
    ]


def _output(status: Any) -> dict:
    value = _value(status, "output")
    return value if isinstance(value, dict) else {}


def _step_output(step: Any) -> Any:
    return _value(step, "output", _value(step, "result"))


def _queue_position(dbos: Any, workflow_ids: list[str]) -> int | None:
    getter = getattr(dbos, "list_queued_workflows", None)
    if not getter:
        return None
    queued = getter(queue_name="codex") or []
    for position, row in enumerate(queued, 1):
        if (
            _value(row, "workflow_id", row if isinstance(row, str) else None)
            in workflow_ids
        ):
            return position
    return None


def _short_sha(value: Any) -> Any:
    """Commit shas render at 8 characters everywhere on this surface.

    The server owns every string the client shows, so truncation happens here
    rather than in the renderer. A full 40 character sha in a nowrap meta line
    ellipsizes into something unreadable, and the contract fixtures are
    explicit that these fields are short.
    """
    if not isinstance(value, str):
        return value
    return value[:8]


def _finding(code: str | None, observed: str | None, prior: str | None) -> dict | None:
    if not code and observed is None and prior is None:
        return None
    if code == "error":
        text = "session start failed: embervm returned 502 Bad Gateway"
    elif observed is None:
        text = "the branch was not found on the remote after the turn completed, so the work was not pushed"
        code = code or "branch_missing"
    elif prior == observed:
        text = "the branch exists on the remote but its head did not move during the turn; retried with push feedback"
        code = code or "head_unchanged"
    else:
        text = "the branch head moved during this attempt"
        code = code or "head_advanced"
    return {"code": code, "text": text, "observed_head": _short_sha(observed)}


def _branch_head_observations(steps: list[Any]) -> list[Any]:
    """Every read_branch_head output in the run, in call order.

    `implement_then_review` calls it only inside the implement loop, twice per
    attempt: once before the turn and once after (`workflows.py:166,180`). The
    list is therefore implement-lane evidence in `[prior, observed]` pairs, and
    belongs to no other node.
    """
    observations = []
    for step in steps:
        name = str(_value(step, "function_name", _value(step, "name", "")))
        if "read_branch_head" in name:
            observations.append(_step_output(step))
    return observations


def _attempts(
    session_rows: list[Any], observations: list[Any], node_key: str
) -> list[dict]:
    """Attempts for one node, paired against observations that node produced.

    Observations are passed in rather than derived from the run's steps.
    Deriving them here gave every node the same undifferentiated list, so the
    review node, which produces no head reads at all, was annotated with
    implement attempt 1's pair and displayed its finding verbatim. Branch-head
    movement is the routing signal in ADR 038 decision 1, so evidence attached
    to the wrong node is the exact confusion that decision exists to prevent.
    A caller that has no observations for a node passes none.
    """
    rows = sorted(
        [r for r in session_rows if _value(r, "node_key") == node_key],
        key=lambda r: _value(r, "node_attempt", 0) or 0,
    )
    result = []
    for i, row in enumerate(rows):
        prior = observations[i * 2] if i * 2 < len(observations) else None
        observed = observations[i * 2 + 1] if i * 2 + 1 < len(observations) else None
        if isinstance(prior, dict):
            prior = prior.get("head", prior.get("sha"))
        if isinstance(observed, dict):
            observed = observed.get("head", observed.get("sha"))
        state = _value(row, "status", "running")
        attempt_state = "failed" if state == "warn" else state
        result.append(
            {
                "n": _value(row, "node_attempt"),
                "session_id": _value(row, "id"),
                "local_session_id": _value(row, "local_session_id"),
                "model": _value(row, "model"),
                "state": "gated"
                if state == "completed"
                and observed == prior
                and (prior is not None or observed is not None)
                else attempt_state,
                "started_at": _iso(_value(row, "created_at")),
                "ended_at": _iso(_value(row, "last_turn_at"))
                if state != "running"
                else None,
                "cost_usd": _value(row, "total_cost_usd", _value(row, "cost_usd")),
                "finding": _finding(None, observed, prior),
                "rationale": parse_rationale(_value(row, "final_result_text")),
                "prior_head": _short_sha(prior),
                "live": None
                if state != "running"
                else {
                    "activity": _value(row, "activity"),
                    "observed_at": _iso(
                        _value(row, "activity_observed_at", _value(row, "last_turn_at"))
                    ),
                },
            }
        )
    return result


def _plan(attrs: dict) -> dict:
    pinned = attrs.get("plan")
    if isinstance(pinned, dict):
        return {"pinned": True, "source": "recorded", **pinned}
    return {
        "pinned": False,
        "source": "current_deploy",
        "max_attempts": None,
        "implementer_model": config.implementer_model(),
        "reviewer_model": config.reviewer_model(),
        "turn_timeout_seconds": None,
        "version": 1,
        "budget_usd": None,
    }


def _derived_state(
    dbos_status: str, output: dict, nodes: list[dict], stranded: bool
) -> str:
    if dbos_status == "CANCELLED":
        return "cancelled"
    if stranded:
        return "stranded"
    if dbos_status == "SUCCESS":
        if output.get("status") == "escalated":
            return "escalated"
        verdict = output.get("review_verdict") or output.get("verdict")
        if verdict == "approve":
            return "approved"
        if verdict == "request_changes":
            return "changes_requested"
        return "escalated"
    if any(n["state"] == "queued" for n in nodes):
        return "queued"
    if any(n["key"] == "review" and n["state"] == "running" for n in nodes):
        return "reviewing"
    if any(n["state"] == "blocked" for n in nodes):
        return "blocked"
    return "running"


def _structured_verdict(output: dict, repo: str | None) -> dict | None:
    from swarm.policy import parse_review_verdict

    # If already parsed, use that; otherwise parse from text
    value = output.get("review_verdict")
    verdict_text = output.get("review_text", "")

    if not value and verdict_text:
        value = parse_review_verdict(verdict_text)

    if not value:
        return None

    summary_plain = {
        "approve": "approved the changes",
        "request_changes": "requested changes",
        "blocked": "blocked the review",
        "unparseable": "verdict could not be parsed",
    }.get(value, "verdict unclear")

    commit_sha = output.get("commit_sha")
    return {
        "value": value,
        "summary_plain": summary_plain,
        "text_md": verdict_text,
        "commit_sha": _short_sha(commit_sha),
        "commit_url": f"https://github.com/{repo or 'unknown/repo'}/commit/{commit_sha}"
        if commit_sha
        else None,
    }


def _event_stream(
    workflow_id,
    dbos_status,
    created_at,
    implement_attempts,
    review_attempts,
    output,
    repo,
    stranded,
):
    events = []

    def add(at, register, text, refs):
        timestamp = _iso(at)
        if timestamp:
            events.append(
                {"at": timestamp, "register": register, "text": text, "refs": refs}
            )

    add(
        created_at,
        "fact",
        f"started run {_short_sha(workflow_id)}",
        {"workflow_id": workflow_id},
    )
    for attempt in implement_attempts:
        if attempt.get("started_at"):
            add(
                attempt["started_at"],
                "fact",
                f"implement attempt {attempt['n']} started with {attempt['model']}",
                {
                    "node": "implement",
                    "attempt": attempt["n"],
                    "session_id": attempt["session_id"],
                },
            )
        rationale = attempt.get("rationale") or {}
        if rationale.get("raw"):
            add(
                attempt.get("ended_at") or attempt.get("started_at"),
                "testimony",
                f"implementer reported (attempt {attempt['n']})",
                {
                    "node": "implement",
                    "attempt": attempt["n"],
                    "session_id": attempt["session_id"],
                    "rationale_key": "paths",
                },
            )
        if attempt.get("ended_at"):
            if attempt.get("state") == "gated":
                add(
                    attempt["ended_at"],
                    "fact",
                    f"attempt {attempt['n']} head did not move, passed to gate for decision",
                    {
                        "node": "implement",
                        "attempt": attempt["n"],
                        "prior_head": attempt.get("prior_head"),
                    },
                )
            elif attempt.get("state") in ("completed", "failed"):
                observed = (attempt.get("finding") or {}).get("observed_head")
                text = (
                    f"attempt {attempt['n']} advanced to {observed}"
                    if observed
                    else f"attempt {attempt['n']} complete"
                )
                add(
                    attempt["ended_at"],
                    "fact",
                    text,
                    {
                        "node": "implement",
                        "attempt": attempt["n"],
                        "session_id": attempt["session_id"],
                        "sha": observed,
                    },
                )
    for attempt in review_attempts:
        if attempt.get("started_at"):
            add(
                attempt["started_at"],
                "fact",
                f"review attempt {attempt['n']} started with {attempt['model']}",
                {
                    "node": "review",
                    "attempt": attempt["n"],
                    "session_id": attempt["session_id"],
                },
            )
    verdict = _structured_verdict(output, repo)
    if verdict and review_attempts:
        attempt = review_attempts[-1]
        add(
            attempt.get("ended_at") or attempt.get("started_at"),
            "testimony",
            f"reviewer {verdict['summary_plain']}",
            {
                "node": "review",
                "attempt": attempt["n"],
                "session_id": attempt["session_id"],
                "verdict_value": verdict["value"],
                "commit_sha": verdict["commit_sha"],
                "commit_url": verdict["commit_url"],
            },
        )
    if dbos_status == "CANCELLED":
        add(
            output.get("completed_at") or created_at,
            "fact",
            "run cancelled",
            {"workflow_id": workflow_id},
        )
    indexed = enumerate(events)
    return [
        event for _, event in sorted(indexed, key=lambda item: (item[1]["at"], item[0]))
    ]


def _disposition(dbos_status, state, output, nodes, plan):
    if dbos_status == "CANCELLED":
        return {"state": "cancelled", "reason": "this run was cancelled", "next": None}
    if state == "approved":
        return {
            "state": "approved",
            "reason": "the reviewer approved the changes",
            "next": None,
        }
    if state == "changes_requested":
        if output.get("status") == "review_cycles_exhausted":
            return {
                "state": "review_cycles_exhausted",
                "reason": "the maximum number of review cycles was reached",
                "next": "amend the plan to retry",
            }
        return {
            "state": "changes_requested",
            "reason": "the reviewer requested changes",
            "next": "awaiting plan amendment or manual retry",
        }
    if state == "escalated":
        return {
            "state": "escalated",
            "reason": "the run was escalated, awaiting human review",
            "next": None,
        }
    if state in ("running", "queued", "reviewing", "blocked"):
        return {"state": "running", "reason": "this run is in progress", "next": None}
    return {"state": state, "reason": f"state: {state}", "next": None}


def compose_run(dbos, workflow_id, session_rows, server_app_version) -> dict | None:
    workflow = dbos.retrieve_workflow(workflow_id)
    if workflow is None:
        return None
    status = _status(workflow)
    if status is None:
        return None
    attrs = _attributes(workflow, status)
    if not attrs:
        getter = getattr(dbos, "get_workflow_attributes", None)
        if getter:
            attrs = getter(workflow_id) or {}
    raw_value = _value(status, "status", "PENDING")
    raw = str(_value(raw_value, "value", raw_value))
    inp = _input(status)
    output = _output(status)
    app_version = _value(
        status, "app_version", attrs.get("app_version", inp.get("app_version"))
    )
    # Fail safe. Stranding is a terminal claim ("will not resume unaided"), so
    # it must be positive evidence that the two versions differ, never the
    # absence of evidence. When either side is missing the honest answer is
    # "cannot tell", and an unknown that renders as a diagnosis is how every
    # pending run came to carry a stranded banner while it was still running.
    stranded = (
        raw in ("PENDING", "ENQUEUED")
        and bool(app_version)
        and bool(server_app_version)
        and app_version != server_app_version
    )
    steps = _steps(dbos, workflow_id)
    children = _children(dbos, workflow_id)
    plan = _plan(attrs)
    model_i = plan["implementer_model"]
    model_r = plan["reviewer_model"]
    sessions = list(session_rows or [])
    # Only the implement lane reads the branch head, so only it gets the
    # observations. The reviewer runs after the gate has already decided on
    # them and never takes a reading of its own.
    implement_attempts = _attempts(
        sessions, _branch_head_observations(steps), "implement"
    )
    review_attempts = _attempts(sessions, [], "review")
    first = implement_attempts[0] if implement_attempts else None
    if raw == "CANCELLED":
        implement_state = "failed" if first else "cancelled"
        gate_state, review_state = "cancelled", "cancelled"
    elif output.get("status") == "escalated":
        implement_state, gate_state, review_state = "escalated", "refused", "cancelled"
    elif output.get("review_verdict") == "unparseable":
        # parse_review_verdict is fail-closed and says so: "routing must never
        # guess a verdict". Painting the review node done would put a check
        # mark on an outcome the server explicitly does not know. Implement and
        # the gate are still facts (the head moved, which is why review ran).
        implement_state, gate_state, review_state = "done", "passed", "escalated"
    elif output.get("review_verdict") in {"approve", "request_changes", "blocked"}:
        # The review node ran to completion in all three cases. Which way it
        # went is the run's state and the verdict box, not the node's.
        implement_state, gate_state, review_state = "done", "passed", "done"
    elif review_attempts and _value(review_attempts[-1], "state") == "running":
        implement_state, gate_state, review_state = "done", "passed", "running"
    elif implement_attempts and _value(implement_attempts[-1], "state") == "running":
        implement_state, gate_state, review_state = "running", "waiting", "future"
    else:
        implement_state, gate_state, review_state = "future", "future", "future"
    if stranded:
        gate_state = "waiting"
    child_ids = [
        _value(_status(child), "workflow_id", _value(child, "workflow_id"))
        for child in children
    ]
    position = _queue_position(dbos, [workflow_id, *[x for x in child_ids if x]])
    if position and implement_state == "future":
        implement_state = "queued"
    gate_decision = None
    if gate_state in ("waiting", "passed") and not stranded:
        gate_decision = {
            "register": "fact" if plan["pinned"] else "belief",
            "basis": "policy.next_action"
            if plan["pinned"]
            else "policy.next_action with an unrecorded retry bound",
            "outcomes": (
                [
                    {"when": "head moved", "then": f"review ({model_r})"},
                    {"when": "head unchanged", "then": "escalate, no attempts left"},
                    {"when": "turn times out", "then": "escalate, no attempts left"},
                ]
                if plan["pinned"]
                else [
                    {"when": "head moved", "then": f"review ({model_r})"},
                    {
                        "when": "head unchanged",
                        "then": "retry or escalate; the bound was not recorded for this run",
                    },
                ]
            ),
        }
    nodes = [
        {
            "key": "implement",
            "kind": "work",
            "label": "implement",
            "state": implement_state,
            "model": model_i,
            "attempts": implement_attempts,
            "queue": {"name": "codex", "position": position} if position else None,
            "decision": None,
            "verdict": None,
            "note": None,
            "deps": [],
            "blocked_on": None,
            "evidence": None,
        },
        {
            "key": "push_gate",
            "kind": "gate",
            "label": "push gate",
            "state": gate_state,
            "model": None,
            "attempts": [],
            "queue": None,
            "decision": gate_decision,
            "verdict": None,
            "note": None,
            "deps": ["implement"],
            "blocked_on": None,
            "evidence": {
                "kind": "branch_head",
                "summary": (
                    f"head {_short_sha(output.get('branch_head'))} "
                    "observed on the remote after the turn"
                ),
            }
            if output.get("branch_head")
            else None,
        },
        {
            "key": "review",
            "kind": "work",
            "label": "review",
            "state": review_state,
            "model": model_r,
            "attempts": review_attempts,
            "queue": None,
            "decision": None,
            "verdict": _structured_verdict(output, inp.get("repo")),
            "note": "never dispatched: nothing was verified for review"
            if review_state == "cancelled" and gate_state == "refused"
            else None,
            "deps": ["push_gate"],
            "blocked_on": None,
            "evidence": None,
        },
    ]
    cost_usd = sum(
        float(_value(row, "total_cost_usd", _value(row, "cost_usd", 0)) or 0)
        for row in sessions
    )
    state = _derived_state(raw, output, nodes, stranded)
    disposition = _disposition(raw, state, output, nodes, plan)
    events = _event_stream(
        workflow_id,
        raw,
        _value(status, "created_at"),
        implement_attempts,
        review_attempts,
        output,
        inp.get("repo"),
        stranded,
    )
    work_branch = output.get("work_branch") or f"claude/swarm-{workflow_id}"
    return {
        "workflow_id": workflow_id,
        "dbos_status": raw,
        "state": state,
        "disposition": disposition,
        "events": events,
        "task": {
            "text": inp.get("task", ""),
            "repo": inp.get("repo"),
            "base_branch": inp.get("branch"),
        },
        "work_branch": work_branch,
        "branch_url": f"https://github.com/{inp.get('repo')}/tree/{work_branch}",
        "created_at": _iso(_value(status, "created_at")),
        "updated_at": _iso(_value(status, "updated_at")),
        "completed_at": _iso(_value(status, "completed_at")),
        "app_version": app_version,
        "server_app_version": server_app_version,
        "stranded": stranded,
        "cost_usd": cost_usd,
        "note": None,
        "plan": plan,
        "nodes": nodes,
        "deviations": compute_deviations(
            {"state": state, "cost_usd": cost_usd, "plan": plan, "nodes": nodes}
        ),
        "cancelled_by": attrs.get("cancelled_by"),
    }


def compose_master(
    dbos, active, session_costs, server_app_version, limit: int = 50
) -> dict:
    limit = max(1, min(50, limit))
    kwargs = {"has_parent": False, "load_input": True}
    if active:
        kwargs["status"] = ["PENDING", "ENQUEUED"]
    else:
        kwargs.update(limit=limit, sort_desc=True)
    statuses = list(dbos.list_workflows(**kwargs) or [])
    runs = []
    started = time.perf_counter() if not active else None
    for workflow in statuses:
        status = _status(workflow)
        wf_id = _value(status, "workflow_id", _value(workflow, "workflow_id"))
        rows = session_costs.get(wf_id, []) if isinstance(session_costs, dict) else []
        run = compose_run(dbos, wf_id, rows, server_app_version)
        if not run:
            continue
        current = next(
            (
                n
                for n in run["nodes"]
                if n["state"]
                in ("running", "queued", "blocked", "escalated", "waiting")
            ),
            run["nodes"][-1],
        )
        created = run["created_at"]
        elapsed = None
        if created:
            try:
                elapsed = int(
                    (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(created.replace("Z", "+00:00"))
                    ).total_seconds()
                )
            except (TypeError, ValueError):
                pass
        needs = None
        if run["state"] == "escalated":
            needs = {
                "kind": "escalated",
                "reason": current.get("note")
                or f"{current['label']} needs a human look",
            }
        elif run["state"] == "stranded":
            needs = {
                "kind": "stranded",
                "reason": f"started on build {run['app_version']}, server is on {run['server_app_version']}; will not resume unaided",
            }
        elif (
            current["state"] == "blocked"
            and (current.get("blocked_on") or {}).get("kind") == "human"
        ):
            needs = {"kind": "human", "reason": "waiting on your decision"}
        runs.append(
            {
                "workflow_id": wf_id,
                "dbos_status": run["dbos_status"],
                "state": run["state"],
                "title": run["task"]["text"].splitlines()[0],
                "current": {"label": current["label"], "state": current["state"]},
                "elapsed_seconds": elapsed,
                "bound_seconds": run["plan"]["turn_timeout_seconds"]
                if run["plan"]["pinned"]
                else None,
                "cost_usd": run["cost_usd"],
                "active": run["dbos_status"] in ("PENDING", "ENQUEUED"),
                "shape": [
                    {"key": node["key"], "kind": node["kind"], "state": node["state"]}
                    for node in run["nodes"]
                ],
                "needs": needs,
                "queue_position": current["queue"]["position"]
                if current["queue"]
                else None,
                "updated_at": run["updated_at"],
            }
        )
    if started is not None:
        logger.info(
            "compose_master active=false loop completed in %.2f ms (%d workflows)",
            (time.perf_counter() - started) * 1000,
            len(statuses),
        )
    queued = getattr(dbos, "list_queued_workflows", lambda **_: [])(queue_name="codex")
    waiting = len(queued or [])
    running = sum(1 for run in runs if run["current"]["state"] == "running")
    return {
        "queues": [
            {
                "name": "codex",
                "concurrency": config.codex_concurrency(),
                "running": running,
                "waiting": waiting,
            }
        ],
        "runs": runs,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
