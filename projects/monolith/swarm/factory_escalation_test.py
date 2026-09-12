"""A delivery pause that leaves the lane, and the decision that re-admits it.

The defect these cover is #6041: a planner that needed a human emitted
``pause``, the receipt stayed admitted behind ``task_paused`` holding a
delivery slot, and the operator's answer on the issue never reached the next
planner, because the planner's context is the issue body captured at
admission. So a resume replayed the same pause.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import factory_decisions as decisions
from swarm import factory_landing as landing
from swarm import graph
from swarm.factory_intake import admit_next, receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
)
from swarm.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

REPO = "owner/repo"
ISSUE = 7
SECOND_ISSUE = 8


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'escalation.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        SwarmTask,
        SwarmPlanVersion,
        SwarmPlanNode,
        SwarmNodeRun,
        SwarmConductorCall,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        FactoryReviewVerdict,
        FactoryStart,
        FactoryAudit,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, controls, graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine, raising=False)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "0")
    yield engine
    engine.dispose()


class Github:
    """A GitHub that records every write, and pages its own comments back."""

    def __init__(self):
        self.comments: list[dict] = []
        self.writes: list[tuple[str, str, dict]] = []
        self.next_issue = 100

    def list(self, _repo, suffix):
        if "comments" in suffix and "page=1" in suffix:
            return list(self.comments)
        return []

    def write(self, repo, suffix, payload, *, method="POST"):
        self.writes.append((method, suffix, payload))
        if suffix.endswith("/comments"):
            self.comments.append(
                {
                    "body": payload["body"],
                    "html_url": f"https://github.com/{repo}/issues/7#c"
                    f"{len(self.comments) + 1}",
                }
            )
            return self.comments[-1]
        if suffix == "issues":
            self.next_issue += 1
            return {"number": self.next_issue}
        return {}

    def bodies(self) -> str:
        return "\n".join(comment["body"] for comment in self.comments)


@pytest.fixture
def github(monkeypatch):
    fake = Github()
    monkeypatch.setattr(conductor, "github_list", fake.list)
    monkeypatch.setattr(landing, "github_write", fake.write)
    return fake


@pytest.fixture
def notices(monkeypatch):
    from agent import notify as notify_module

    sent = []

    async def notify(text, *, level):
        sent.append((text, level))

    monkeypatch.setattr(notify_module, "notify", notify)
    return sent


def policy_for(*issues):
    return {
        "repo": REPO,
        "issue_numbers": list(issues),
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 0},
        "max_turns_per_task": 20,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 4,
    }


def configure(*issues):
    policy = policy_for(*issues)
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    return policy


def receive(number=ISSUE, title="Deliver the thing"):
    receive_issue(
        REPO,
        number,
        title,
        "Untrusted issue body.",
        f"https://github.com/{REPO}/issues/{number}",
        "operator",
        task_class="bug-fix",
    )


def admitted(*issues):
    """One admitted delivery task, with the policy it was pinned under."""
    policy = configure(*issues)
    for number in issues:
        receive(number)
    result = admit_next("test")
    assert result["ok"], result
    return result["task_id"], policy


def pause_options():
    return [
        {
            "key": "continue-narrowed",
            "label": "Deliver only the /invoke path, leave the console",
            "effect": "agent-ready",
            "detail": {"scope": "Only the /invoke path."},
        },
        {
            "key": "close-superseded",
            "label": "Close it: superseded by #5656",
            "effect": "close",
            "detail": {"reason": "not_planned", "comment": "Superseded by #5656."},
        },
    ]


def pause(options=None, **extra):
    decision = {
        "action": "pause",
        "reason": "The scope covers two surfaces and the issue names one.",
        "question": "Which surface is in scope?",
        **extra,
    }
    if options is not None:
        decision["options"] = options
    return decision


def planner_run(task_id, decision, *, node_key="conductor_1", attempt=1):
    """One succeeded planner attempt carrying ``decision`` as its artifact."""
    return {
        "id": 1,
        "task_id": task_id,
        "node_key": node_key,
        "attempt": attempt,
        "status": "succeeded",
        "outcome_json": json.dumps({"value": decision}),
    }


def task_of(task_id):
    return conductor._task(task_id)


def receipt_of(db, task_id=None, receipt_id=None):
    with Session(db) as session:
        if receipt_id is not None:
            return session.get(FactoryReceipt, receipt_id)
        return session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).one()


def audits(db, action):
    with Session(db) as session:
        return [
            json.loads(row.detail_json)
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id)
            )
        ]


def escalate(db, github, notices, *, issues=(ISSUE,), options=None):
    """Run one planner pause through to a settled escalation."""
    task_id, policy = admitted(*issues)
    decision = pause(pause_options() if options is None else options)
    run = planner_run(task_id, decision)
    conductor.apply_decision(task_of(task_id), policy, run, [run])
    return task_id, policy


def test_a_pause_without_options_is_refused_and_the_task_stays_in_the_lane(
    db, github, notices
):
    """A pause is a decision request, so it has to arrive decidable.

    Refusing it as decision feedback rather than settling it is what lets the
    planner repair the same pause inside the task it already has.
    """
    task_id, policy = admitted(ISSUE)
    run = planner_run(task_id, pause())
    conductor.apply_decision(task_of(task_id), policy, run, [run])
    refusals = audits(db, "conductor_rejected")
    assert [refusal["refusal_code"] for refusal in refusals] == [
        "pause_options_invalid"
    ]
    assert "carries no options" in refusals[0]["reason"]
    assert receipt_of(db, task_id).state == "admitted"
    assert github.writes == []
    assert notices == []


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ([pause_options()[0]], "not two to four"),
        (pause_options() + pause_options(), "repeats an option key"),
        (
            [
                {"key": "a", "label": "Close it", "effect": "close", "detail": {}},
                pause_options()[0],
            ],
            "closes with no reason",
        ),
        # Option one is what resume applies without showing anyone the card,
        # so a pause that recommends closing would turn pressing resume into
        # closing the issue.
        (list(reversed(pause_options())), "must be agent-ready"),
        (
            [
                {"key": "hold-it", "label": "Leave it", "effect": "hold"},
                pause_options()[0],
            ],
            "must be agent-ready",
        ),
    ],
)
def test_a_pause_whose_options_do_not_hold_up_is_refused(
    db, github, notices, options, expected
):
    task_id, policy = admitted(ISSUE)
    run = planner_run(task_id, pause(options))
    conductor.apply_decision(task_of(task_id), policy, run, [run])
    refusals = audits(db, "conductor_rejected")
    assert refusals and expected in refusals[0]["reason"]
    assert receipt_of(db, task_id).state == "admitted"


def test_a_pause_with_no_question_is_refused_rather_than_raising(db, github, notices):
    """The schema asks for it, and the server never trusts the schema alone."""
    task_id, policy = admitted(ISSUE)
    decision = pause(pause_options())
    del decision["question"]
    run = planner_run(task_id, decision)
    conductor.apply_decision(task_of(task_id), policy, run, [run])
    refusals = audits(db, "conductor_rejected")
    assert [refusal["refusal_code"] for refusal in refusals] == [
        "pause_without_question"
    ]
    assert receipt_of(db, task_id).state == "admitted"


def test_the_planner_prompt_states_the_pause_option_contract(db, github, notices):
    task_id, policy = admitted(ISSUE)
    conductor.reconcile_task(task_id, policy, object())
    node = next(
        node for node in graph.load_graph(task_id) if node["node_key"] == "conductor_1"
    )
    for said in (
        "the first option must have effect `agent-ready`",
        "Any other first effect is refused",
        "rescope to a named surface",
        "Deliver only the /invoke path, leave the console",
        "re-admits this issue as a NEW task",
    ):
        assert said in node["prompt"], said


def test_the_decision_schema_demands_a_question_and_options_from_a_pause():
    from swarm.turn_artifact import schema_errors

    assert schema_errors(
        {"action": "pause", "reason": "scope is unclear"}, conductor.DECISION_SCHEMA
    )
    assert not schema_errors(pause(pause_options()), conductor.DECISION_SCHEMA)


def test_a_pause_settles_escalated_and_leaves_the_lane(db, github, notices):
    """The slot, the label, the card, the warn, and the graph that survives."""
    task_id, _policy = escalate(db, github, notices)
    receipt = receipt_of(db, task_id)
    assert receipt.state == "escalated"
    # Nothing is paused: there is no task left in the lane to pause.
    assert receipt.task_paused is False
    document = json.loads(receipt.escalation_json)
    assert document["question"] == "Which surface is in scope?"
    assert document["reason"].startswith("The scope covers two surfaces")
    assert [option["key"] for option in document["options"]] == [
        "continue-narrowed",
        "close-superseded",
    ]
    assert document["branch"] == f"factory/{task_id}"
    assert document["resolved"] is None
    assert document["recommendation"] == "deliver"
    # The label goes on before the card, so the issue is never decidable and
    # re-admittable at the same time.
    assert [(write[0], write[1]) for write in github.writes] == [
        ("POST", f"issues/{ISSUE}/labels"),
        ("POST", f"issues/{ISSUE}/comments"),
    ]
    assert github.writes[0][2] == {"labels": ["needs-human"]}
    body = github.bodies()
    assert "## Decision needed" in body
    assert "1. **Deliver only the /invoke path, leave the console** (deliver)" in body
    assert document["comment_url"] == github.comments[0]["html_url"]
    assert len(notices) == 1
    assert notices[0][1] == "warn"
    assert "agents/escalations" in notices[0][0]
    assert audits(db, "conductor_escalated")[0]["cause"].startswith(
        "factory-decision:conductor_1"
    )


def test_an_escalated_task_frees_its_slot_for_the_next_delivery(db, github, notices):
    """The whole point: a queued delivery is admitted on the next tick.

    At a ceiling of one, a pause that held its slot behind ``task_paused``
    stopped the lane dead until a person came back.
    """
    task_id, _policy = escalate(db, github, notices, issues=(ISSUE, SECOND_ISSUE))
    assert admit_next("test")["ok"]
    with Session(db) as session:
        states = {
            row.issue_number: row.state for row in session.exec(select(FactoryReceipt))
        }
    assert states[ISSUE] == "escalated"
    assert states[SECOND_ISSUE] == "admitted"
    # And the escalated task keeps everything a decision will be read against.
    assert controls.task_snapshot(task_id)["escalation"]["options"]


def test_a_second_escalation_supersedes_the_first_without_losing_it(
    db, github, notices
):
    """One receipt is re-admitted under its own identity, so it can ask twice."""
    task_id, policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    decisions.apply_decision(receipt_id, "continue-narrowed", "joe@example.test")
    assert admit_next("test")["ok"]
    second = receipt_of(db, receipt_id=receipt_id).task_id
    run = planner_run(second, pause(pause_options()))
    conductor.apply_decision(task_of(second), policy, run, [run])
    document = json.loads(receipt_of(db, receipt_id=receipt_id).escalation_json)
    assert document["task_id"] == second and document["resolved"] is None
    assert [entry["task_id"] for entry in document["history"]] == [task_id]
    assert document["history"][0]["resolved"]["option_key"] == "continue-narrowed"


def test_a_decision_re_admits_the_work_with_the_operator_direction(db, github, notices):
    """The answer reaches the next planner, which is what #6041 was about."""
    task_id, policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    result = decisions.apply_decision(
        receipt_id,
        "continue-narrowed",
        "joe@example.test",
        "Console is #6100, do not touch it.",
    )
    assert result["resolution"]["effects"]["readmitted"] is True
    row = receipt_of(db, receipt_id=receipt_id)
    assert row.state == "queued" and row.task_id is None
    direction = json.loads(row.direction_json)
    assert direction["option_key"] == "continue-narrowed"
    assert direction["prior_task_id"] == task_id
    assert direction["prior_branch"] == f"factory/{task_id}"

    admitted_again = admit_next("test")
    assert admitted_again["ok"]
    second = admitted_again["task_id"]
    assert second != task_id
    assert controls.operator_direction(second)["note"].startswith("Console is #6100")
    # A fresh graph, not the escalated task's nodes.
    assert graph.load_graph(second) == []

    conductor.reconcile_task(second, policy, object())
    node = next(
        node for node in graph.load_graph(second) if node["node_key"] == "conductor_1"
    )
    context = json.loads(node["prompt"].rsplit("\n", 1)[1])
    assert context["operator_direction"]["option_key"] == "continue-narrowed"
    assert context["operator_direction"]["effect"] == "agent-ready"
    assert context["operator_direction"]["note"].startswith("Console is #6100")
    assert context["operator_direction"]["previous_branch"] == f"factory/{task_id}"
    assert context["operator_direction"]["answering"] == "Which surface is in scope?"
    assert "operator_direction, when it is present" in node["prompt"]
    assert audits(db, "operator_direction_read")[0]["previous_task_id"] == task_id


def test_the_direction_is_carried_on_the_first_round_only(db, github, notices):
    """Later rounds read the plan it shaped, and the feedback under that."""
    task_id, policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    decisions.apply_decision(receipt_id, "continue-narrowed", "joe@example.test")
    second = admit_next("test")["task_id"]
    runs = [planner_run(second, pause(pause_options()))]
    prompt = conductor.planner_prompt(
        task_of(second),
        graph.load_graph(second),
        runs,
        operator_direction=(
            controls.operator_direction(second)
            if not any(run["node_key"].startswith("conductor_") for run in runs)
            else None
        ),
    )
    assert json.loads(prompt.rsplit("\n", 1)[1])["operator_direction"] is None


def test_an_ending_decision_settles_the_receipt_rather_than_re_admitting_it(
    db, github, notices
):
    """Cancelled, not succeeded: nothing was delivered.

    A succeeded delivery receipt is in the exclusion intake keeps forever, so
    marking a held or closed escalation succeeded would take the issue off the
    lane for good on a decision that never said to.
    """
    task_id, _policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    decisions.apply_decision(receipt_id, "close-superseded", "joe@example.test")
    row = receipt_of(db, receipt_id=receipt_id)
    assert row.state == "cancelled" and row.task_id == task_id
    assert row.direction_json is None
    # The task itself keeps escalated: that is what happened to it, and the
    # receipt is the lane's record of what a person decided afterwards.
    with Session(db) as session:
        assert session.get(SwarmTask, task_id).start_state == "escalated"


def test_a_decision_the_lane_cannot_admit_says_so_instead_of_stranding_it(
    db, github, notices
):
    """An answered card that schedules nothing is the state this replaced."""
    task_id, _policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    moved = policy_for(ISSUE)
    moved["generation"] = 3
    with Session(db) as session:
        control = session.get(FactoryControl, "factory")
        control.policy_json = json.dumps(moved)
        session.add(control)
        session.commit()
    result = decisions.apply_decision(
        receipt_id, "continue-narrowed", "joe@example.test"
    )
    effects = result["resolution"]["effects"]
    assert effects["readmitted"] is False
    assert "generation" in effects["blocked_by"]
    assert receipt_of(db, receipt_id=receipt_id).state == "cancelled"


def test_the_chat_action_asks_on_the_issue_and_re_admits_with_the_note(
    db, github, notices
):
    task_id, _policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    result = decisions.request_chat(
        receipt_id, "Use the existing client, do not write a second one.", "joe@x.test"
    )
    assert result["requeued"] is True
    assert "Operator asks: Use the existing client" in github.bodies()
    assert "Re-admitted to the delivery lane" in github.bodies()
    row = receipt_of(db, receipt_id=receipt_id)
    assert row.state == "queued" and row.task_id is None
    direction = json.loads(row.direction_json)
    assert direction["effect"] == "chat"
    assert direction["note"].startswith("Use the existing client")
    assert direction["prior_task_id"] == task_id
    second = admit_next("test")["task_id"]
    assert controls.operator_direction(second)["effect"] == "chat"


def test_deciding_after_a_chat_keeps_the_delivery_on_its_direction(db, github, notices):
    """A chat re-queues without resolving, so an option can land on a queued
    receipt. Reading that as the advisory re-brief case settled it succeeded,
    which is the state intake reads as delivered for good, and the direction
    the chat wrote was replaced by nothing.
    """
    task_id, _policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    assert decisions.request_chat(receipt_id, "Which client?", "joe@x.test")["requeued"]
    result = decisions.apply_decision(
        receipt_id, "continue-narrowed", "joe@example.test", "Use the existing one."
    )
    assert result["resolution"]["effects"]["readmitted"] is True
    row = receipt_of(db, receipt_id=receipt_id)
    assert row.state == "queued"
    direction = json.loads(row.direction_json)
    assert direction["option_key"] == "continue-narrowed"
    assert direction["note"] == "Use the existing one."
    # The chat cleared task_id, so the branch and the prior task come from the
    # direction the chat itself left rather than from a row that no longer
    # names them.
    assert direction["prior_task_id"] == task_id
    assert direction["prior_branch"] == f"factory/{task_id}"
    assert direction["previous_task_ids"] == [task_id]
    second = admit_next("test")["task_id"]
    assert controls.operator_direction(second)["note"] == "Use the existing one."
    # The audit names the task the decision was made against, not the null a
    # re-queued receipt carries.
    assert audits(db, "decision_applied")[-1]["receipt_id"] == receipt_id


def test_an_ending_decision_after_a_chat_cancels_rather_than_succeeding(
    db, github, notices
):
    task_id, _policy = escalate(db, github, notices)
    receipt_id = receipt_of(db, task_id).id
    assert decisions.request_chat(receipt_id, "Which client?", "joe@x.test")["requeued"]
    decisions.apply_decision(receipt_id, "close-superseded", "joe@example.test")
    row = receipt_of(db, receipt_id=receipt_id)
    assert row.state == "cancelled"
    # Never succeeded: that is the state intake's delivered rule reads as this
    # issue being done for good.
    assert admit_next("test")["ok"] is False


def test_the_board_keeps_what_the_escalated_attempt_spent(db, github, notices):
    """A re-admission mints a new task, so the old one's cost has to be kept.

    Kept beside the current task's accounting rather than added into it: the
    limits are measured against this task's own allowance, and folding a
    previous attempt's spend in would trip every one of them before a node ran.
    """
    task_id, policy = admitted(ISSUE)
    # Spend a work turn before the planner asks, so there is something to keep.
    granted = controls.authorize_start(
        task_id, "one", "worker", model="luna", max_cost_usd=2.0
    )
    assert granted["ok"], granted
    assert controls.record_start_outcome(
        task_id, "one", "succeeded", "worker", cost_usd=1.75, session_id=1
    )["ok"]
    run = planner_run(task_id, pause(pause_options()))
    conductor.apply_decision(task_of(task_id), policy, run, [run])
    receipt_id = receipt_of(db, task_id).id
    assert receipt_of(db, task_id).state == "escalated"
    decisions.apply_decision(receipt_id, "continue-narrowed", "joe@example.test")
    second = admit_next("test")["task_id"]
    snapshot = controls.task_snapshot(second)
    assert snapshot["previous_task_ids"] == [task_id]
    assert snapshot["previous_spend"] == {
        "turns_used": 1,
        "committed_cost_usd": 1.75,
        "attempts": 1,
    }
    # The new task starts on its own clean allowance.
    assert snapshot["turns_used"] == 0
    assert snapshot["committed_cost_usd"] == 0
    assert snapshot["limits"]["budget_limit_reached"] is False


def test_resume_by_hand_applies_the_recommended_option(db, github, notices):
    """The first option is the recommendation, so resume is choosing it."""
    task_id, _policy = escalate(db, github, notices)
    result = controls.set_control("resume_task", "joe@example.test", task_id=task_id)
    assert result["ok"], result
    assert result["resolution"]["option_key"] == "continue-narrowed"
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
    assert row.state == "queued"
    assert json.loads(row.direction_json)["option_key"] == "continue-narrowed"
    assert json.loads(row.escalation_json)["resolved"]["actor"] == "joe@example.test"


def test_resume_on_a_task_that_is_still_running_is_unchanged(db, github, notices):
    task_id, _policy = admitted(ISSUE)
    assert controls.set_control("pause_task", "operator", task_id=task_id)["ok"]
    assert receipt_of(db, task_id).task_paused is True
    assert controls.set_control("resume_task", "operator", task_id=task_id)["ok"]
    assert receipt_of(db, task_id).task_paused is False
    assert receipt_of(db, task_id).state == "admitted"
    assert github.writes == []


def test_stop_settles_an_escalated_receipt_as_cancelled(db, github, notices):
    """A shut lane must not leave a card waiting on a decision for it."""
    task_id, _policy = escalate(db, github, notices)
    assert controls.set_control("stop", "operator")["ok"]
    row = receipt_of(db, task_id)
    assert row.state == "cancelled" and row.cancellation_requested is True
    with Session(db) as session:
        assert session.get(SwarmTask, task_id).start_state == "escalated"


def test_both_kinds_of_escalation_render_side_by_side(db, github, notices):
    """The page reads one list, so a delivery card has to carry its own facts."""
    task_id, _policy = escalate(db, github, notices)
    receipts = controls.status()["receipts"]
    views = controls.escalations(receipts)
    assert len(views) == 1
    delivery = views[0]
    assert delivery["kind"] == "delivery"
    assert delivery["task_class"] == "bug-fix"
    assert delivery["branch"] == f"factory/{task_id}"
    assert delivery["open"] is True
    # An escalated receipt runs nothing, so the buttons are live.
    assert delivery["briefing"] is False
    assert [option["effect"] for option in delivery["options"]] == [
        "agent-ready",
        "close",
    ]
    assert delivery["reason"].startswith("The scope covers two surfaces")

    # A refine escalation beside it, shaped by the same view.
    receive_issue(
        REPO,
        SECOND_ISSUE,
        "Clarify the ask",
        "",
        f"https://github.com/{REPO}/issues/{SECOND_ISSUE}",
        "factory:intake",
        task_class="refine",
    )
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.issue_number == SECOND_ISSUE)
        ).one()
        row.state = "succeeded"
        row.escalation_json = json.dumps(
            {
                "recommendation": "defer",
                "question": "Is this still wanted?",
                "options": [
                    {"key": "defer", "label": "Defer it", "effect": "defer"},
                    {"key": "hold", "label": "Leave it", "effect": "hold"},
                ],
                "resolved": None,
            }
        )
        session.add(row)
        session.commit()
    kinds = {
        view["issue_number"]: view["kind"]
        for view in controls.escalations(controls.status()["receipts"])
    }
    assert kinds == {ISSUE: "delivery", SECOND_ISSUE: "advisory"}


def test_the_card_and_the_label_are_written_once_across_retries(db, github, notices):
    """Settlement is re-reached until it takes, so the writes have to be fenced."""
    task_id, policy = admitted(ISSUE)
    run = planner_run(task_id, pause(pause_options()))
    calls = {"n": 0}
    real = controls.finish_task

    def refuse_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "reason": "unresolved_starts"}
        return real(*args, **kwargs)

    controls.finish_task, saved = refuse_once, controls.finish_task
    try:
        conductor.apply_decision(task_of(task_id), policy, run, [run])
        assert receipt_of(db, task_id).state == "admitted"
        conductor.apply_decision(task_of(task_id), policy, run, [run])
    finally:
        controls.finish_task = saved
    assert receipt_of(db, task_id).state == "escalated"
    assert len(github.comments) == 1
    assert len(notices) == 1
