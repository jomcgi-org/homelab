"""Explicit operator recovery of a settled evidence-only correction owner."""

from datetime import datetime, timedelta
import json

from sqlmodel import select

from factory.orchestration import evidence_continuation as continuation
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTION = "operator_evidence_continuation"


def _snapshot(db, control, request):
    from factory.orchestration import factory_decisions, factory_task_recovery
    from factory.orchestration.factory_funding_limits import pending

    row = db.get(FactoryReceipt, request["receipt_id"])
    continuation._require(
        row is not None and row.task_id is not None, "receipt_missing"
    )
    continuation._require(
        row.state == "escalated"
        and not row.task_paused
        and not row.cancellation_requested
        and not controls.decision_in_flight(db, row.id),
        "recovery_held",
    )
    escalation = json.loads(row.escalation_json or "{}")
    continuation._require(
        escalation
        and not escalation.get("resolved")
        and factory_decisions._fields(row)["decision_id"] == request["decision_id"],
        "decision_changed",
    )
    raw_deadline = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
    continuation._require(
        raw_deadline.tzinfo is not None, "recovery_deadline_requires_timezone"
    )
    deadline = continuation._aware(raw_deadline)
    continuation._require(
        controls._now() < deadline <= controls._now() + timedelta(hours=1),
        "recovery_deadline_outside_window",
    )
    factory_task_recovery._capacity(db, control, row)
    continuation._require(pending(db, row.task_id) is None, "funding_pending")
    continuation._require(
        controls._landing_recovery_deadline(db, row.task_id) is None, "other_recovery"
    )
    continuation._require(
        not controls.task_recovery_grant(row.task_id, session=db), "other_recovery"
    )
    continuation._require(
        not controls.review_recovery_held(row.task_id, session=db), "other_recovery"
    )
    local = continuation._local(
        db, control, row.task_id, request["decision"], terminal=True
    )
    succeeded = {r["node_key"] for r in local["runs"] if r["status"] == "succeeded"}
    continuation._require(
        all(
            n["node_key"] in (local["node"]["node_key"], local["review"]["node_key"])
            or n["node_key"] in succeeded
            for n in local["nodes"]
        ),
        "other_unfinished_work",
    )
    continuation._require(local["task"]["settled_at"] is not None, "not_settled")
    snapshot = controls._snapshot(db, row)
    continuation._require(
        snapshot["planner_turns_used"] < controls.planner_turn_cap(local["policy"]),
        "planner_cap",
    )
    # The old planner decisions stay processed. Only this named next planner
    # can perform final acceptance, after the existing pair both completes.
    planner_key = f"conductor_{sum(n['node_key'].startswith('conductor_') for n in local['nodes']) + 1}"
    return {
        "local": local,
        "control_version": control.version,
        "accounting": controls._accounting(controls._starts(db, row.task_id)),
        "planner_key": planner_key,
        "planner_turn_ceiling": snapshot["planner_turns_used"] + 1,
        "deadline_at": deadline.isoformat(),
        "prior_escalation": escalation,
    }


def _proposal(request):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.turn_artifact import schema_errors

    continuation._require(
        not schema_errors(request["decision"], conductor.DECISION_SCHEMA)
        and request["decision"]["action"] == continuation.OP,
        "invalid_request",
    )
    with controls._locked_session() as (db, control):
        snapshot = _snapshot(db, control, request)
    local = snapshot["local"]
    evidence = continuation.observe(
        local["task"],
        {**request["decision"], "operator_decision_id": request["decision_id"]},
        github_get=conductor.github_get,
    )
    grant, allowance = continuation.make_grant(
        local,
        request["decision"],
        evidence,
        "operator-evidence:" + request["decision_id"],
    )
    grant["operator_decision_id"] = request["decision_id"]
    proposal = {
        **snapshot,
        "grant": grant,
        "allowance": allowance,
        "work_turn_ceiling": allowance["turns"],
        "reactivates_same_receipt_and_task": True,
        "allowed_starts": {
            grant["node_key"]: grant["next_attempt"],
            grant["review_key"]: 1,
            snapshot["planner_key"]: 1,
        },
        "historical_result_and_accounting_unchanged": True,
    }
    # Canonical JSON gives preview and apply identical datetime representations.
    proposal = json.loads(json.dumps(proposal, default=str))
    proposal["preview_sha256"] = continuation._hash(proposal)
    return proposal


def preview(request):
    proposal = _proposal(request)
    local = proposal["local"]
    return {
        "ok": True,
        "proposal": {k: v for k, v in proposal.items() if k != "local"},
        "scope": {
            "receipt_id": local["receipt_id"],
            "task_id": local["task"]["id"],
            "graph_revision": local["version"],
            "policy": local["policy"],
            "source_run_id": local["source"]["id"],
            "prior_settled_at": local["task"]["settled_at"],
        },
    }


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == ACTION, FactoryAudit.actor == actor
        )
    ).all():
        old = json.loads(raw)
        if old["request"]["request_key"] == request["request_key"]:
            continuation._require(old["request"] == request, "request_conflict")
            return old["result"]
    return None


def apply(request, *, actor, request_key, expected_preview_sha256, delegation=None):
    identity = {
        **request,
        "request_key": controls._text(request_key, "request_key", 256),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._read_session() as db:
        prior = _replay(db, actor, identity)
        if prior is not None:
            return prior
    proposal = _proposal(request)
    continuation._require(
        proposal["preview_sha256"] == expected_preview_sha256, "preview_changed"
    )
    with controls._locked_session() as (db, control):
        prior = _replay(db, actor, identity)
        if prior is not None:
            return prior
        current = json.loads(json.dumps(_snapshot(db, control, request), default=str))
        continuation._require(
            all(proposal[k] == v for k, v in current.items()), "state_changed"
        )
        from factory.orchestration.recovery_delegation import guard

        guard(
            db,
            actor,
            "evidence_continuation",
            request,
            expected_preview_sha256,
            delegation,
        )
        local, grant = proposal["local"], proposal["grant"]
        row = db.get(FactoryReceipt, request["receipt_id"])
        task = graph._lock_task(db, row.task_id)
        continuation.commit(db, local, grant)
        # Preserve the original decision and settlement in the immutable audit;
        # this is a distinct operator resolution, never a selected readmission.
        row.escalation_json = json.dumps(
            {
                **proposal["prior_escalation"],
                "resolved": {
                    "effect": ACTION,
                    "actor": actor,
                    "decision_id": request["decision_id"],
                    "request_key": request_key,
                    "decided_at": controls._now().isoformat(),
                },
            }
        )
        row.state, row.updated_at = "admitted", controls._now()
        row.allowance_json = json.dumps(proposal["allowance"])
        task.start_state, task.settled_at = "factory", None
        task.start_updated_at = controls._now()
        control.version += 1
        control.actor, control.updated_at = actor, controls._now()
        result = {
            "ok": True,
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "preview_sha256": expected_preview_sha256,
            "deadline_at": proposal["deadline_at"],
            "allowed_starts": proposal["allowed_starts"],
            "work_turn_ceiling": proposal["work_turn_ceiling"],
            "planner_turn_ceiling": proposal["planner_turn_ceiling"],
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=row.task_id,
            request=identity,
            proposal=proposal,
            result=result,
        )
        return result


def reconcile(task, policy, nodes, runs):
    """Own only the granted pair and one final acceptance, never re-plan it."""
    from factory.orchestration import factory_conductor as conductor

    with controls._read_session() as db:
        grant = continuation.recovery_grant(db, task["id"])
    if grant is None:
        return False
    pair = grant["grant"]
    by_key = {n["node_key"]: n for n in nodes}
    for key, attempt in (
        (pair["node_key"], pair["next_attempt"]),
        (pair["review_key"], 1),
    ):
        latest = next(
            (r for r in runs if r["node_key"] == key and r["attempt"] == attempt), None
        )
        if latest is None:
            if key not in by_key or by_key[key].get("evidence_continuation_blocked"):
                conductor._pause_refusal(
                    task,
                    "Evidence continuation changed; no replacement work authorized",
                )
            else:
                conductor._dispatch_ready(
                    task, nodes, runs, 1, fan_out=False, parallel=1, policy=policy
                )
            return True
        if latest["status"] != "succeeded" or (
            key == pair["review_key"]
            and conductor._artifact(latest).get("verdict") != "approve"
        ):
            conductor._pause_refusal(
                task,
                "Evidence continuation did not obtain independent approval; grant exhausted",
            )
            return True
    final = [r for r in runs if r["node_key"] == grant["planner_key"]]
    if final:
        run = final[0]
        decision = conductor._artifact(run)
        cause = f"factory-decision:{run['node_key']}:{run['attempt']}"
        if (
            run["status"] == "succeeded"
            and decision.get("action") == "finish"
            and not decision.get("gate")
        ):
            if not conductor._decision_processed(task["id"], cause):
                conductor.apply_decision(task, policy, run, runs)
            with controls._read_session() as db:
                rejected = any(
                    json.loads(raw).get("cause") == cause
                    for raw in db.exec(
                        select(FactoryAudit.detail_json).where(
                            FactoryAudit.task_id == task["id"],
                            FactoryAudit.action == "conductor_rejected",
                        )
                    ).all()
                )
            if rejected:
                conductor._pause_refusal(
                    task,
                    "Evidence continuation final acceptance refused; grant exhausted",
                )
        else:
            conductor._pause_refusal(
                task,
                "Evidence continuation final acceptance did not complete; grant exhausted",
            )
        return True
    if grant["planner_key"] in by_key:
        final_nodes = [
            {**n, "evidence_acceptance": pair}
            if n["node_key"] == grant["planner_key"]
            else n
            for n in nodes
        ]
        conductor._dispatch_ready(
            task, final_nodes, runs, 1, fan_out=False, parallel=1, policy=policy
        )
        return True
    revision = graph.current_version(task["id"])
    prompt = conductor.planner_prompt(
        task,
        nodes,
        runs,
        decision_revision=revision + 1,
        operator_direction={
            "effect": ACTION,
            "note": "Only final exact-head acceptance (finish) or pause is authorized. No new work, funding, graph edits or source changes.",
        },
    )
    choice = conductor.select_model("conductor", policy)
    result = conductor._add(
        task,
        policy,
        grant["planner_key"],
        prompt,
        [],
        choice["model"],
        "evidence-acceptance:" + grant["planner_key"],
        "One final acceptance for the operator's evidence continuation",
        planner=True,
        max_attempts=1,
        expected_version=revision,
    )
    if result.ok:
        conductor._record_allowance(task["id"], policy, "evidence-final-acceptance")
    else:
        conductor._pause_refusal(
            task, "Evidence continuation acceptance insertion refused"
        )
    return True
