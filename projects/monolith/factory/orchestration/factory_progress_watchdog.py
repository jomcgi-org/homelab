"""The no-progress watchdog: spend that produced nothing reaches a person.

A task's budget is an escalation lever, not a stop. This watchdog is the
earlier lever: once an admitted delivery task has spent another
``progress_watchdog.threshold_usd`` (default $20) without pushing a new commit
or reporting a new pull request since it was admitted or last cleared, the
orchestrator assesses it once for that crossing.

The assessment is deterministic first. A refusal code recurring three times,
a node key that was added again and failed again, three failures with the same
reason, or three implementation attempts with no new commit between them are
loops on their face and short-circuit without a model call. Anything else goes
to one bounded chat-inference call (the classifier's endpoint and model,
minimal reasoning effort, a bounded token cap, list priced), which answers
``progressing`` or ``looping``. An unreadable answer is asked once more. Only
two unreadable answers fail closed to ``looping``: the task has spent the
threshold with nothing to show and nothing vouching for it, which is exactly
when a person should look.

``progressing`` records an audit row and re-arms at the next step. ``looping``
fences the task with the existing ``pause_task`` control, under this actor so
the reconciler's own pause expiry never cancels it, and raises a decision card
(Resume, Stop, Rescope) on the receipt, the issue and Discord. The card is
decided on the escalations page like any other; the answer is enacted here on
the next tick. A plain ``resume_task`` works too. Either way the watchdog
re-arms from the spend at the moment of clearance, so a legitimate task gets
another full step before it is asked again. A delivery (a new commit or pull
request) clears the watchdog the same way.

All state is derived from the audit trail, so there is no migration and a
restart loses nothing.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import logging
import os
import re
import time

import httpx
from sqlmodel import select

from factory.orchestration import graph
from factory.orchestration.tracing import set_attributes, tracer

logger = logging.getLogger(__name__)

ACTOR = "factory:watchdog"
KIND = "watchdog"
ASSESSED = "watchdog_assessed"
CLEARED = "watchdog_cleared"
ENACT_REFUSED = "watchdog_enact_refused"
PROGRESSING = "progressing"
LOOPING = "looping"

# Deterministic loop evidence. Each is a shape no amount of further spend
# fixes by itself, so none of them needs a model to recognise.
REPEATED_REFUSALS = 3
READDED_FAILURES = 2
IDENTICAL_FAILURES = 3
IMPLEMENT_ATTEMPTS_WITHOUT_COMMIT = 3
_IMPLEMENTATION = re.compile(r"^(?:implement|correct|integrate)_")
_SHA = re.compile(r"^[0-9a-f]{40}$")

# The model call: one request, bounded in time and tokens, over a bounded
# slice of history. Muse Spark list price puts it well under a cent.
HISTORY_LIMIT = 30
TEXT_CHARS = 240
MODEL_TIMEOUT_SECONDS = 20.0
# Spark always reasons, and reasoning tokens count against max_tokens. At 512
# with the provider's default effort both 2026-10-01 assessments (#6529,
# #6530) spent 509 of 512 tokens reasoning and returned no verdict, so every
# model assessment failed closed to a page. Minimal effort keeps the
# reasoning short; the larger ceiling leaves the verdict room when it is not.
MODEL_MAX_TOKENS = 2048
MODEL_REASONING_EFFORT = "minimal"
# One retry before failing closed. A second unreadable answer still pages.
MODEL_ATTEMPTS = 2

RESUME_OPTION = "resume"
STOP_OPTION = "stop"
RESCOPE_OPTION = "rescope"

_SYSTEM = (
    "You are the factory orchestrator's no-progress watchdog. A delivery task "
    "has spent past its watchdog threshold without pushing a new commit or "
    "opening a pull request. From its recent node history decide whether it "
    "is still converging on a delivery (progressing) or spending without "
    "converging (looping): the same node re-added, repeated refusals, "
    "identical failures, attempts that change nothing. Reply with one JSON "
    'object and nothing else: {"verdict": "progressing" | "looping", '
    '"reason": "<one sentence>", "evidence": ["<short fact>", ...]}'
)


def _controls():
    from factory.orchestration import factory_controls

    return factory_controls


@contextmanager
def _session():
    # The control module's session seam, so the watchdog reads the same
    # database the rest of the lane does (and every test that swaps it).
    with _controls()._read_session() as db:
        yield db


# ---------------------------------------------------------------------------
# Reading the state


def _detail(row) -> dict:
    try:
        value = json.loads(row.detail_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _trail(db, task_id: str) -> list:
    from factory.orchestration.factory_models import FactoryAudit

    return list(
        db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action.in_(
                    (
                        ASSESSED,
                        CLEARED,
                        "pause_task",
                        "resume_task",
                        "conductor_rejected",
                        "dispatch_refused",
                    )
                ),
            )
            .order_by(FactoryAudit.id)
        ).all()
    )


def state(trail: list, threshold: float) -> dict:
    """Where the watchdog stands, read from its own audit rows.

    ``next_threshold_usd`` is the spend that triggers the next assessment,
    ``run_watermark`` the last node run already accounted for, and
    ``audit_watermark`` the last audit row, so refusals are counted since the
    same point. ``open`` is the looping assessment still waiting on a person.
    """
    result = {
        "next_threshold_usd": threshold,
        "run_watermark": 0,
        "audit_watermark": 0,
        "open": None,
        "assessment_cost_usd": 0.0,
        "last": None,
    }
    for row in trail:
        if row.action not in (ASSESSED, CLEARED):
            continue
        detail = _detail(row)
        result["assessment_cost_usd"] += float(detail.get("cost_usd") or 0.0)
        result["last"] = {"id": row.id, "action": row.action, **detail}
        if row.action == CLEARED:
            result.update(
                next_threshold_usd=detail["next_threshold_usd"],
                run_watermark=detail["run_watermark"],
                audit_watermark=row.id,
                open=None,
            )
        elif detail.get("verdict") == LOOPING:
            result["open"] = {"id": row.id, **detail}
        else:
            result["next_threshold_usd"] = detail["next_threshold_usd"]
    return result


def spend(db, task_id: str, assessment_cost_usd: float = 0.0) -> float:
    """Committed spend of every settled start, plus the watchdog's own calls.

    A start still reserved is an attempt in flight; its reservation is a
    ceiling, not spend, and counting it would trip the watchdog on the first
    large dispatch.
    """
    controls = _controls()
    settled = [s for s in controls._starts(db, task_id) if s.status != "reserved"]
    return round(
        sum(controls._committed_cost(s) for s in settled) + assessment_cost_usd, 6
    )


def _artifact(run: dict) -> dict:
    try:
        outcome = json.loads(run.get("outcome_json") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(outcome, dict):
        return {}
    value = outcome.get("value") or outcome.get("artifact") or {}
    return value if isinstance(value, dict) else {}


def _reason(run: dict) -> str:
    try:
        outcome = json.loads(run.get("outcome_json") or "{}")
    except (TypeError, ValueError):
        outcome = {}
    if not isinstance(outcome, dict):
        outcome = {}
    artifact = _artifact(run)
    said = (
        outcome.get("reason")
        or outcome.get("error")
        or artifact.get("reason")
        or artifact.get("summary")
        or artifact.get("verdict")
        or ""
    )
    return " ".join(str(said).split())[:TEXT_CHARS]


def _delivery_marks(run: dict) -> set[str]:
    """The commits and pull requests one attempt reports having produced."""
    artifact = _artifact(run)
    marks = set()
    for sha in (artifact.get("head_sha"), run.get("head_sha")):
        if isinstance(sha, str) and _SHA.fullmatch(sha) and sha != run.get("base_sha"):
            marks.add(f"sha:{sha}")
    number = artifact.get("pr_number")
    if type(number) is int and number > 0:
        marks.add(f"pr:{number}")
    return marks


def delivered_since(runs: list[dict], watermark: int) -> list[str]:
    """New commits or pull requests reported after ``watermark``.

    The same signal the escalation card and task detail read a pull request
    from (a succeeded attempt's artifact), widened to the pushed head: a mark
    is new when no attempt at or before the watermark reported it.
    """
    seen = set()
    for run in runs:
        if run["id"] <= watermark:
            seen |= _delivery_marks(run)
    fresh = []
    for run in sorted(runs, key=lambda item: item["id"]):
        if run["id"] <= watermark or run["status"] != "succeeded":
            continue
        for mark in sorted(_delivery_marks(run) - seen):
            fresh.append(mark)
            seen.add(mark)
    return fresh


# ---------------------------------------------------------------------------
# Assessing


def _plan_adds(db, task_id: str) -> Counter:
    from factory.orchestration.models import SwarmPlanVersion

    adds = Counter()
    for change in db.exec(
        select(SwarmPlanVersion.change_json).where(
            SwarmPlanVersion.task_id == task_id, SwarmPlanVersion.op == "add_node"
        )
    ).all():
        try:
            key = json.loads(change).get("node_key")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(key, str):
            adds[key] += 1
    return adds


def evidence(runs: list[dict], trail: list, adds: Counter, watch: dict) -> dict:
    """The recent history an assessment reads, since the last clearance."""
    recent = [r for r in runs if r["id"] > watch["run_watermark"]]
    refusals = [
        _detail(row).get("refusal_code")
        for row in trail
        if row.id > watch["audit_watermark"]
        and row.action in ("conductor_rejected", "dispatch_refused")
    ]
    history = [
        {
            "node_key": r["node_key"],
            "attempt": r["attempt"],
            "status": r["status"],
            "cost_usd": round(float(r.get("accounted_cost_usd") or 0.0), 4),
            "head_sha": (_artifact(r).get("head_sha") or r.get("head_sha") or "")[:12]
            or None,
            "said": _reason(r) or None,
        }
        for r in sorted(recent, key=lambda item: item["id"])[-HISTORY_LIMIT:]
    ]
    return {
        "runs": recent,
        "all_runs": runs,
        "history": history,
        "refusals": [code for code in refusals if isinstance(code, str) and code],
        "adds": adds,
        "active": [
            r for r in runs if r["status"] in ("admitted", "dispatched", "uncertain")
        ],
    }


def deterministic_loop(found: dict) -> tuple[str, list[str]] | None:
    """A loop visible without a model, as (reason, evidence), or None."""
    counts = Counter(found["refusals"])
    for code, count in counts.most_common():
        if count >= REPEATED_REFUSALS:
            return (
                f"refusal {code} recurred {count} times without a delivery",
                [f"refusal_code={code} x{count}"],
            )
    failed = [r for r in found["runs"] if r["status"] == "failed"]
    per_key = Counter(r["node_key"] for r in failed)
    for key, count in per_key.most_common():
        if count >= READDED_FAILURES and found["adds"].get(key, 0) >= 2:
            return (
                (
                    f"node {key} was re-added {found['adds'][key]} times and "
                    f"failed {count} times"
                ),
                [f"node_key={key} added x{found['adds'][key]} failed x{count}"],
            )
    signatures = Counter(
        (r["node_key"].rstrip("0123456789"), _reason(r)) for r in failed if _reason(r)
    )
    for (prefix, said), count in signatures.most_common():
        if count >= IDENTICAL_FAILURES:
            return (
                f"{count} attempts failed identically: {said[:120]}",
                [f"{prefix}* failed x{count}: {said[:120]}"],
            )
    settled = [
        r
        for r in found["runs"]
        if _IMPLEMENTATION.match(r["node_key"])
        and r["status"] in ("succeeded", "failed")
    ]
    if len(settled) >= IMPLEMENT_ATTEMPTS_WITHOUT_COMMIT:
        keys = ", ".join(f"{r['node_key']}#{r['attempt']}" for r in settled[-5:])
        return (
            f"{len(settled)} implementation attempts produced no new commit",
            [f"implementation attempts without a commit: {keys}"],
        )
    return None


def _content_text(content: object) -> str | None:
    """The message text, whether the endpoint sent a string or content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) or None
    return None


def _parse(text: str | None) -> dict | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    verdict = str(value.get("verdict") or "").strip().lower()
    if verdict not in (PROGRESSING, LOOPING):
        return None
    reason = " ".join(str(value.get("reason") or "").split())[:500]
    if not reason:
        return None
    facts = value.get("evidence")
    facts = [str(f)[:200] for f in facts[:8]] if isinstance(facts, list) else []
    return {"verdict": verdict, "reason": reason, "evidence": facts}


def _ask(
    url: str, model: str, prompt: str, system: str = _SYSTEM
) -> tuple[dict | None, float, str | None]:
    """One request: (parsed verdict or None, priced cost, why it was unreadable)."""
    import shared.inference
    from shared.pricing import price_usage

    response = httpx.post(
        f"{url}/v1/chat/completions",
        headers=shared.inference.auth_headers(url),
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": MODEL_MAX_TOKENS,
            "reasoning_effort": MODEL_REASONING_EFFORT,
        },
        timeout=MODEL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    shared.inference.record_usage(usage, model, "factory_watchdog")
    priced = price_usage(
        model,
        {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    )
    cost = round(priced.cost_usd, 6) if priced else 0.0
    choice = (data.get("choices") or [{}])[0]
    parsed = _parse(_content_text((choice.get("message") or {}).get("content")))
    if parsed is not None:
        return parsed, cost, None
    # Say why, so an unreadable verdict is diagnosable from the audit row.
    finish = choice.get("finish_reason")
    details = usage.get("completion_tokens_details") or {}
    why = (
        f"unreadable: finish_reason={finish} "
        f"completion_tokens={usage.get('completion_tokens')} "
        f"reasoning_tokens={details.get('reasoning_tokens')}"
    )
    return None, cost, why


def model_assessment(task: dict, found: dict, spent: float, threshold: float) -> dict:
    """At most two bounded chat-inference calls. Never raises.

    An unreadable answer is asked once more; only a second one fails closed
    to looping.
    """
    import shared.inference

    from factory.orchestration import factory_judge

    model = shared.inference.META_SPARK_MODEL
    url = os.environ.get("LLAMA_CPP_URL", "")
    brief = {
        "task": {
            "title": str(task.get("title") or "")[:300],
            "issue_number": task.get("issue_number"),
        },
        "spend_usd": spent,
        "threshold_usd": threshold,
        "attempts_in_flight": len(found["active"]),
        "refusal_codes": found["refusals"][-HISTORY_LIMIT:],
        "node_history": found["history"],
    }
    system = _SYSTEM
    if url and factory_judge.enabled():
        # Judge on the merits the operator would use: verified progress, how
        # close the work is to landing and what it is worth. progressing
        # resumes; looping is the doubt that asks a person.
        brief["merit"] = factory_judge.merit_evidence(
            task, found.get("all_runs") or found["runs"]
        )
        system = (
            f"{_SYSTEM} {factory_judge.CRITERIA} Answer progressing when you "
            "would approve, and looping only when you doubt it."
        )
    prompt = json.dumps(brief, sort_keys=True, default=str)
    result = {"model": model, "cost_usd": 0.0}
    if not url:
        return {
            **result,
            "verdict": LOOPING,
            "reason": "assessment_unavailable: no inference endpoint configured",
            "evidence": [],
        }
    started = time.monotonic()
    parsed = None
    errors = []
    for _attempt in range(MODEL_ATTEMPTS):
        try:
            parsed, cost, error = _ask(url, model, prompt, system)
            result["cost_usd"] = round(result["cost_usd"] + cost, 6)
        except Exception as exc:  # noqa: BLE001 - an unreadable assessment fails closed
            logger.warning("factory watchdog assessment failed for %s", task.get("id"))
            error = f"{type(exc).__name__}: {exc}"[:300]
        if parsed is not None:
            break
        errors.append(error)
    result["attempts"] = len(errors) + (parsed is not None)
    if errors:
        result["error"] = " | ".join(str(e) for e in errors)[:600]
    result["latency_ms"] = max(0, round((time.monotonic() - started) * 1000))
    if parsed is None:
        return {
            **result,
            "verdict": LOOPING,
            "reason": (
                "assessment_unavailable: the model gave no readable verdict "
                f"in {MODEL_ATTEMPTS} attempts"
            ),
            "evidence": [],
        }
    return {**result, **parsed}


def assess(task: dict, found: dict, spent: float, threshold: float) -> dict:
    """Classify the task, deterministically when a loop is plain."""
    with tracer.start_as_current_span("factory.watchdog.assess") as span:
        loop = deterministic_loop(found)
        if loop is not None:
            verdict = {
                "verdict": LOOPING,
                "reason": loop[0],
                "evidence": loop[1],
                "short_circuit": True,
                "model": None,
                "cost_usd": 0.0,
            }
        else:
            verdict = {
                **model_assessment(task, found, spent, threshold),
                "short_circuit": False,
            }
        set_attributes(
            span,
            {
                "factory.task_id": task.get("id"),
                "factory.watchdog.spend_usd": spent,
                "factory.watchdog.threshold_usd": threshold,
                "factory.watchdog.verdict": verdict["verdict"],
                "factory.watchdog.reason": verdict["reason"],
                "factory.watchdog.short_circuit": verdict["short_circuit"],
                "factory.watchdog.cost_usd": verdict["cost_usd"],
            },
        )
        return verdict


# ---------------------------------------------------------------------------
# Intervening


def _record(action: str, task_id: str, **detail) -> None:
    controls = _controls()
    with controls._locked_session() as (db, _control):
        controls._audit(db, ACTOR, action, task_id=task_id, **detail)


def _options(spent: float) -> list[dict]:
    return [
        {
            "key": RESUME_OPTION,
            "label": "Resume: the work is legitimate",
            "effect": "agent-ready",
            "detail": {
                "scope": (
                    "Unpause this task on its existing branch. The no-progress "
                    f"watchdog re-arms from the spend at resume (now ${spent:.2f})."
                )
            },
        },
        {
            "key": STOP_OPTION,
            "label": "Stop: cancel this task, leave the issue open",
            "effect": "hold",
            "detail": {},
        },
        {
            "key": RESCOPE_OPTION,
            "label": "Rescope: cancel this task and defer the issue for rewriting",
            "effect": "defer",
            "detail": {
                "comment": (
                    "Rescope the issue so a fresh task can converge, then relabel "
                    "it to restart."
                )
            },
        },
    ]


def _card(document: dict) -> str:
    from factory.orchestration.factory_refine import ESCALATIONS_URL

    lines = [
        "## Decision needed: no progress",
        "",
        document["question"],
        "",
        document["reason"],
        "",
    ]
    if document.get("evidence"):
        lines += ["Evidence:", ""] + [f"- {fact}" for fact in document["evidence"]]
        lines.append("")
    lines += ["Options:", ""]
    for index, option in enumerate(document["options"], start=1):
        lines.append(f"{index}. **{option['label']}**")
    lines += [
        "",
        (
            "The task is paused: running attempts finish, nothing new starts. "
            f"Decide at {ESCALATIONS_URL}, or resume the task to carry on."
        ),
        "",
        f"Branch `{document['branch']}`"
        + (f", pull request {document['pr_url']}" if document.get("pr_url") else "")
        + ".",
    ]
    return "\n".join(lines)


def _escalate(
    task: dict, verdict: dict, spent: float, threshold: float, ordinal: int, runs
):
    from factory.orchestration import factory_conductor as conductor

    task_id = task["id"]
    question = (
        f"Task {task_id} has spent ${spent:.2f} (watchdog threshold "
        f"${threshold:.2f}) without a new commit or pull request. Resume, stop "
        "or rescope?"
    )
    document = {
        "kind": KIND,
        "task_id": task_id,
        "ordinal": ordinal,
        "recommendation": "resume",
        "question": question,
        "reason": verdict["reason"][:4000],
        "evidence": verdict.get("evidence") or [],
        "summary": "\n".join(verdict.get("evidence") or [])[:1000],
        "options": _options(spent),
        "branch": conductor.delivery_branch(task),
        "pr_number": None,
        "pr_url": None,
        "comment_url": None,
        "downgraded": False,
        "resolved": None,
        "spend_usd": spent,
        "threshold_usd": threshold,
        "short_circuit": verdict["short_circuit"],
    }
    pr_number = conductor._latest_pr(runs) or conductor.delivery_pr_number(task)
    if pr_number:
        document["pr_number"] = pr_number
        document["pr_url"] = f"https://github.com/{task['repo']}/pull/{pr_number}"
    number = task.get("issue_number")
    if isinstance(number, int):
        try:
            document["comment_url"] = conductor._post_decision_card(
                task["repo"],
                number,
                f"<!-- factory-watchdog:{task_id}:{ordinal} -->",
                _card(document),
            )
        except Exception:  # noqa: BLE001 - the pause and the page still stand
            logger.warning("factory watchdog card post failed for %s", task_id)
    conductor._record_escalation(task_id, document)
    conductor._notify_person_once(
        task_id,
        f"Factory watchdog paused task {task_id} ({task.get('repo')}#{number}): "
        f"${spent:.2f} spent with no new commit or pull request. "
        f"{verdict['reason'][:500]}",
        kind=f"watchdog:{ordinal}",
    )


def _clear(task_id: str, reason: str, spent: float, threshold: float, runs, **extra):
    _record(
        CLEARED,
        task_id,
        reason=reason,
        spend_usd=spent,
        next_threshold_usd=round(spent + threshold, 6),
        run_watermark=max((r["id"] for r in runs), default=0),
        threshold_usd=threshold,
        **extra,
    )


def _resolve_card(task_id: str, actor: str, note: str) -> None:
    """Mark the open watchdog card answered when the task was resumed directly."""
    controls = _controls()
    from factory.orchestration.factory_models import FactoryReceipt

    with controls._locked_session() as (db, _control):
        row = db.exec(
            select(FactoryReceipt)
            .where(FactoryReceipt.task_id == task_id)
            .execution_options(populate_existing=True)
        ).first()
        document = (
            json.loads(row.escalation_json) if row and row.escalation_json else None
        )
        if (
            not isinstance(document, dict)
            or document.get("kind") != KIND
            or document.get("resolved") is not None
        ):
            return
        option = document["options"][0]
        document["resolved"] = {
            "option_key": option["key"],
            "label": option["label"],
            "effect": option["effect"],
            "actor": actor,
            "note": note,
            "effects": {"resumed": True},
            "decided_at": controls._now().isoformat(),
            "decision_id": controls.decision_identity(
                {
                    "id": row.id,
                    "repo": row.repo,
                    "generation": row.generation,
                    "escalation": json.loads(row.escalation_json),
                }
            ),
        }
        row.escalation_json = json.dumps(document)
        row.updated_at = controls._now()
        db.add(row)


def _enact(task: dict, open_: dict, receipt, trail, spent, threshold, runs) -> str:
    """Carry out what a person decided about an open watchdog pause."""
    controls = _controls()
    task_id = task["id"]
    pause_id = max(
        (r.id for r in trail if r.action == "pause_task" and r.actor == ACTOR),
        default=open_["id"],
    )
    resumed = next(
        (
            r
            for r in trail
            if r.action == "resume_task" and r.id > pause_id and _detail(r).get("ok")
        ),
        None,
    )
    document = json.loads(receipt.escalation_json) if receipt.escalation_json else {}
    resolved = (
        (document or {}).get("resolved") if document.get("kind") == KIND else None
    )
    choice = (resolved or {}).get("option_key")
    if resumed is None and choice == RESUME_OPTION:
        result = controls.set_control("resume_task", ACTOR, task_id=task_id)
        if not result["ok"]:
            _record(ENACT_REFUSED, task_id, option_key=choice, refusal=result["reason"])
            return "enact_refused"
        resumed = True
    if resumed is not None:
        actor = resolved.get("actor") if resolved else getattr(resumed, "actor", ACTOR)
        if resolved is None:
            _resolve_card(task_id, actor, "Resumed with resume_task.")
        _clear(task_id, "resume", spent, threshold, runs, resumed_by=actor)
        return "cleared"
    if choice in (STOP_OPTION, RESCOPE_OPTION):
        result = controls.finish_task(
            task_id,
            "cancelled",
            ACTOR,
            evidence={
                "state": f"watchdog_{choice}",
                "reason": f"No-progress watchdog: operator chose {choice}.",
            },
        )
        if not result["ok"]:
            # Most often an attempt still in flight. The next tick retries.
            return "enact_waiting"
        return "stopped"
    return "awaiting_person"


def check(task_id: str, policy: dict) -> str:
    """One tick of the watchdog for one active task. Returns what it did."""
    controls = _controls()
    watch_policy = controls.progress_watchdog_policy(policy)
    if not watch_policy["enabled"]:
        return "disabled"
    threshold = watch_policy["threshold_usd"]
    from factory.orchestration.factory_models import FactoryReceipt

    with _session() as db:
        receipt = db.exec(
            select(FactoryReceipt)
            .where(FactoryReceipt.task_id == task_id)
            .execution_options(populate_existing=True)
        ).first()
        if (
            receipt is None
            or receipt.state not in ("admitted", "uncertain")
            or receipt.cancellation_requested
            or controls.is_advisory(controls.receipt_task_class(receipt))
        ):
            return "not_watched"
        trail = _trail(db, task_id)
        watch = state(trail, threshold)
        spent = spend(db, task_id, watch["assessment_cost_usd"])
        runs = graph.node_runs(task_id, session=db)
        task = {
            "id": task_id,
            "repo": receipt.repo,
            "issue_number": receipt.issue_number,
            "title": receipt.title,
        }
        task_paused = receipt.task_paused
        adds = _plan_adds(db, task_id) if spent >= watch["next_threshold_usd"] else None
        db.expunge(receipt)
    if watch["open"] is not None:
        return _enact(
            task,
            watch["open"],
            receipt,
            trail,
            spent,
            threshold,
            runs,
        )
    if spent < watch["next_threshold_usd"]:
        return "under_threshold"
    fresh = delivered_since(runs, watch["run_watermark"])
    if fresh:
        _clear(task_id, "delivery", spent, threshold, runs, delivered=fresh[:10])
        return "cleared"
    if task_paused:
        # Somebody else's fence. Assessing a task that cannot start anything
        # would only pile a second question on the first.
        return "paused_elsewhere"
    found = evidence(runs, trail, adds, watch)
    crossing = watch["next_threshold_usd"]
    if found["active"] and spent < crossing + threshold:
        # An attempt in flight may be the one that pushes. The pause would only
        # fence the next start anyway, so wait for it to settle, unless spend
        # has run a whole further step past the crossing meanwhile.
        return "waiting_for_attempt"
    verdict = assess(task, found, spent, threshold)
    next_threshold = crossing
    while next_threshold <= spent:
        next_threshold = round(next_threshold + threshold, 6)
    ordinal = sum(1 for r in trail if r.action == ASSESSED) + 1
    _record(
        ASSESSED,
        task_id,
        verdict=verdict["verdict"],
        reason=verdict["reason"],
        evidence=verdict.get("evidence") or [],
        short_circuit=verdict["short_circuit"],
        model=verdict.get("model"),
        cost_usd=verdict["cost_usd"],
        error=verdict.get("error"),
        attempts=verdict.get("attempts"),
        spend_usd=spent,
        threshold_usd=threshold,
        crossing_usd=crossing,
        next_threshold_usd=next_threshold,
        ordinal=ordinal,
    )
    if verdict["verdict"] == PROGRESSING:
        return "progressing"
    paused = controls.set_control("pause_task", ACTOR, task_id=task_id)
    if not paused["ok"]:
        logger.warning(
            "factory watchdog could not pause %s: %s", task_id, paused["reason"]
        )
    from factory.orchestration import factory_conductor as conductor

    _escalate(
        {**conductor._task(task_id), **task}, verdict, spent, threshold, ordinal, runs
    )
    return "paused"


def decidable(row, escalation: dict | None) -> bool:
    """A watchdog card is answered while its task is paused, not after it settles."""
    return (
        isinstance(escalation, dict)
        and escalation.get("kind") == KIND
        and escalation.get("task_id") == row.task_id
        and row.state in ("admitted", "uncertain")
        and bool(row.task_paused)
    )


def detail(db, task_id: str | None, policy: dict | None) -> dict | None:
    """The watchdog block for the task detail view."""
    if not task_id:
        return None
    controls = _controls()
    watch_policy = controls.progress_watchdog_policy(policy or {})
    trail = _trail(db, task_id)
    watch = state(trail, watch_policy["threshold_usd"])
    return {
        "enabled": watch_policy["enabled"],
        "threshold_usd": watch_policy["threshold_usd"],
        "spend_usd": spend(db, task_id, watch["assessment_cost_usd"]),
        "next_threshold_usd": watch["next_threshold_usd"],
        "assessment_cost_usd": round(watch["assessment_cost_usd"], 6),
        "paused_by_watchdog": watch["open"] is not None,
        "last": watch["last"],
    }


def paused_receipts(db) -> list[tuple[int, int, float]]:
    """(receipt id, issue, spend) for each active task the watchdog is holding.

    Held means the task's standing pause is the watchdog's own: its latest
    successful pause_task was written by this actor. A person who paused the
    task again by hand after resuming it owns that pause instead.
    """
    from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

    held = []
    rows = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.state.in_(("admitted", "uncertain")),
            FactoryReceipt.task_paused.is_(True),
            FactoryReceipt.task_id.is_not(None),
        )
    ).all()
    for row in rows:
        pauses = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "pause_task",
            )
            .order_by(FactoryAudit.id.desc())
        ).all()
        latest = next((p for p in pauses if _detail(p).get("ok")), None)
        if latest is not None and latest.actor == ACTOR:
            held.append((row.id, row.issue_number, spend(db, row.task_id)))
    return held
