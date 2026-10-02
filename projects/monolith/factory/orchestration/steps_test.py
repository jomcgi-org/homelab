import json

import httpx
import pytest

import factory.orchestration.steps as steps


@pytest.fixture
def turn_wait_database(tmp_path, monkeypatch):
    from sqlmodel import SQLModel, create_engine
    from factory.execution.models import (
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'turn-wait.db'}",
        execution_options={"schema_translate_map": {"agent_sessions": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentSession,
                AgentTurn,
                PendingMessage,
            )
        ],
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize(
    "status,after_seq", [("failed", 0), ("cancelled", 0), ("completed", 1), ("warn", 1)]
)
def test_terminal_session_wait_returns_none_in_two_polls(
    turn_wait_database, monkeypatch, status, after_seq
):
    from sqlmodel import Session
    from factory.execution.models import AgentSession, AgentTurn
    from factory.orchestration import workflows

    with Session(turn_wait_database) as db:
        row = AgentSession(
            local_session_id="terminal", workspace="guest", branch="main", status=status
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        sid = row.id
        if after_seq:
            db.add(
                AgentTurn(
                    session_id=sid,
                    seq=1,
                    prompt="first",
                    result_text="done",
                    terminal_reason="completed",
                )
            )
            db.commit()
    polls = []

    def poll(*args):
        polls.append(args)
        return steps.poll_turn.__wrapped__(*args)

    monkeypatch.setattr(workflows, "poll_turn", poll)
    monkeypatch.setattr(
        workflows,
        "session_turn_wait_terminal",
        steps.session_turn_wait_terminal.__wrapped__,
    )
    monkeypatch.setattr(workflows, "observe_clock", lambda: "2026-10-02T00:00:00+00:00")
    monkeypatch.setattr(workflows.DBOS, "patch", lambda _name: True)
    monkeypatch.setattr(
        workflows.DBOS, "sleep", lambda *_args: pytest.fail("terminal wait slept")
    )
    assert workflows._await_turn(sid, after_seq, 43800) is None
    assert polls == [(sid, after_seq)] * 2


@pytest.mark.parametrize(
    "status,claimed",
    [("running", False), ("running", True), ("completed", False), ("failed", False)],
)
def test_terminal_read_preserves_pending_turns(turn_wait_database, status, claimed):
    from sqlmodel import Session
    from factory.execution.models import AgentSession, PendingMessage

    with Session(turn_wait_database) as db:
        row = AgentSession(
            local_session_id="pending", workspace="guest", branch="main", status=status
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        sid = row.id
        db.add(
            PendingMessage(
                session_id=sid,
                seq=2,
                message_text="correction",
                claimed_by_replica="worker" if claimed else None,
            )
        )
        db.commit()
    assert steps.session_turn_wait_terminal.__wrapped__(sid, 1) is False


def test_running_pending_session_waits_until_deadline(turn_wait_database, monkeypatch):
    from sqlmodel import Session
    from factory.execution.models import AgentSession, PendingMessage
    from factory.orchestration import workflows

    with Session(turn_wait_database) as db:
        row = AgentSession(local_session_id="waiting", workspace="guest", branch="main")
        db.add(row)
        db.commit()
        db.refresh(row)
        sid = row.id
        db.add(PendingMessage(session_id=sid, seq=1, message_text="work"))
        db.commit()
    clock = iter(
        [
            "2026-10-02T00:00:00+00:00",
            "2026-10-02T00:00:01+00:00",
            "2026-10-02T00:00:05+00:00",
        ]
    )
    sleeps = []
    monkeypatch.setattr(workflows, "poll_turn", steps.poll_turn.__wrapped__)
    monkeypatch.setattr(
        workflows,
        "session_turn_wait_terminal",
        steps.session_turn_wait_terminal.__wrapped__,
    )
    monkeypatch.setattr(workflows, "observe_clock", clock.__next__)
    monkeypatch.setattr(workflows.DBOS, "patch", lambda _name: True)
    monkeypatch.setattr(workflows.DBOS, "sleep", sleeps.append)
    assert workflows._await_turn(sid, 0, 5) is None
    assert sleeps == [workflows.POLL_INTERVAL_SECONDS]


@pytest.mark.parametrize("reason", sorted(steps.INTERRUPTED_TERMINAL_REASONS))
def test_terminal_read_preserves_interrupted_replacements(turn_wait_database, reason):
    from sqlmodel import Session
    from factory.execution.models import AgentSession, AgentTurn

    with Session(turn_wait_database) as db:
        row = AgentSession(
            local_session_id="interrupted",
            workspace="guest",
            branch="main",
            status="recovering",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        sid = row.id
        db.add(
            AgentTurn(
                session_id=sid,
                seq=2,
                prompt="correction",
                result_text="partial",
                terminal_reason=reason,
            )
        )
        db.commit()
    assert steps.session_turn_wait_terminal.__wrapped__(sid, 1) is False


def test_terminal_read_detects_missing_session(turn_wait_database):
    assert steps.session_turn_wait_terminal.__wrapped__(999, 0) is True


def test_terminal_read_detects_settled_binding_despite_running_status(
    turn_wait_database,
):
    from sqlmodel import Session
    from factory.execution.models import AgentSession, AgentCapacityReservation

    with Session(turn_wait_database) as db:
        row = AgentSession(
            local_session_id="settled",
            workspace="guest",
            branch="main",
            ember_session_id="guest",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        sid = row.id
        db.add(
            AgentCapacityReservation(
                local_session_id="settled",
                session_id=sid,
                pending_seq=1,
                tier="kg",
                model="luna",
                state="settled",
            )
        )
        db.commit()
    assert steps.session_turn_wait_terminal.__wrapped__(sid, 1) is True


class FakeClient:
    response = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers):
        self.url = url
        self.headers = headers
        return self.response


def test_read_workflow_attributes(monkeypatch):
    class FakeDBOS:
        @staticmethod
        def get_workflow_status(workflow_id):
            assert workflow_id == "wf-1"
            return type("Status", (), {"attributes": {"budget_raises": []}})()

    monkeypatch.setattr(steps, "DBOS", FakeDBOS)

    assert steps.read_workflow_attributes.__wrapped__("wf-1") == {"budget_raises": []}


@pytest.mark.parametrize("status", [None, object()])
def test_read_workflow_attributes_returns_empty_for_missing_attributes(
    monkeypatch, status
):
    class FakeDBOS:
        @staticmethod
        def get_workflow_status(workflow_id):
            return status

    monkeypatch.setattr(steps, "DBOS", FakeDBOS)

    assert steps.read_workflow_attributes.__wrapped__("wf-1") == {}


def test_read_workflow_attributes_returns_empty_on_error(monkeypatch):
    class FakeDBOS:
        @staticmethod
        def get_workflow_status(workflow_id):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(steps, "DBOS", FakeDBOS)

    assert steps.read_workflow_attributes.__wrapped__("wf-1") == {}


def test_pin_plan_resolves_config_once(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("SWARM_IMPLEMENTER_MODEL", "implementer")
    monkeypatch.setenv("SWARM_REVIEWER_MODEL", "reviewer")
    monkeypatch.setenv("SWARM_TURN_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("SWARM_DECISION_TIMEOUT_SECONDS", "84")

    assert steps.pin_plan.__wrapped__(2.0) == {
        "version": 2,
        "max_attempts": 1,
        "max_review_cycles": 2,
        "implementer_model": "implementer",
        "reviewer_model": "reviewer",
        "turn_timeout_seconds": 42,
        "decision_timeout_seconds": 84,
        "budget_usd": 2.0,
    }


def test_pin_plan_uses_implementer_override(monkeypatch):
    monkeypatch.setenv("SWARM_IMPLEMENTER_MODEL", "implementer")
    monkeypatch.setenv("SWARM_REVIEWER_MODEL", "reviewer")

    plan = steps.pin_plan.__wrapped__(2.0, "terra")

    assert plan["implementer_model"] == "terra"
    assert plan["reviewer_model"] == "reviewer"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [(200, {"object": {"sha": "deadbeef"}}, "deadbeef"), (404, {}, None)],
)
def test_read_branch_head(monkeypatch, status, payload, expected):
    request = httpx.Request(
        "GET", "https://api.github.com/repos/jomcgi/homelab/git/ref/heads/swarm/wf-1"
    )
    response = httpx.Response(status, json=payload, request=request)
    FakeClient.response = response
    monkeypatch.setattr(steps.httpx, "Client", FakeClient)

    assert (
        steps.read_branch_head.__wrapped__("jomcgi/homelab", "swarm/wf-1") == expected
    )


def test_read_branch_head_raises_on_server_error(monkeypatch):
    request = httpx.Request(
        "GET", "https://api.github.com/repos/jomcgi/homelab/git/ref/heads/swarm/wf-1"
    )
    FakeClient.response = httpx.Response(500, json={"message": "boom"}, request=request)
    monkeypatch.setattr(steps.httpx, "Client", FakeClient)

    with pytest.raises(httpx.HTTPStatusError):
        steps.read_branch_head.__wrapped__("jomcgi/homelab", "swarm/wf-1")


@pytest.mark.parametrize("tier", ["project", "kg"])
def test_start_agent_session_forwards_workflow_fields(monkeypatch, tier):
    import factory.execution.api as api

    calls = []

    def fake_start_session(*args, **kwargs):
        calls.append((args, kwargs))
        return 101

    monkeypatch.setattr(api, "start_session_for_swarm", fake_start_session)

    result = steps.start_agent_session.__wrapped__(
        "test-key",
        "prompt",
        "luna",
        "jomcgi/homelab",
        "main",
        workflow_id="wf-abc",
        node_key="qwen-drain",
        reasoning=True,
        admission_tier=tier,
    )

    assert result == 101
    assert calls == [
        (
            ("test-key", "prompt", "luna", "jomcgi/homelab", "main"),
            {
                "workflow_id": "wf-abc",
                "node_key": "qwen-drain",
                "node_attempt": None,
                "reasoning": True,
                "admission_tier": tier,
            },
        )
    ]


@pytest.mark.parametrize("stop_reason", ["end_turn", "invocation_outcome_unknown"])
def test_poll_turn_includes_rationale_and_stop_reason(monkeypatch, stop_reason):
    import sqlmodel

    class Query:
        def where(self, *args):
            return self

        def order_by(self, *args):
            return self

    class Result:
        def first(self):
            return type(
                "Turn",
                (),
                {
                    "seq": 2,
                    "prompt_intent": "Implement the fix",
                    "result_text": "Done\n\nRATIONALE\n- path: app.py · why: fix it",
                    "terminal_reason": "error"
                    if stop_reason == "invocation_outcome_unknown"
                    else "completed",
                    "stop_reason": stop_reason,
                    "model": "luna",
                    "cost_usd": 0.25,
                    "list_cost_usd": 0.31,
                    "usage_json": None,
                },
            )()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec(self, query):
            return Result()

    monkeypatch.setattr(sqlmodel, "Session", lambda engine: Session())
    monkeypatch.setattr(sqlmodel, "select", lambda model: Query())
    monkeypatch.setattr("core.db.get_engine", lambda: object())

    payload = steps.poll_turn.__wrapped__(101, 1)

    assert payload["stop_reason"] == stop_reason
    # Codex turns carry no provider cost, so the list price travels with them.
    assert payload["cost_usd"] == 0.25 and payload["list_cost_usd"] == 0.31
    assert payload["rationale"] == {
        "raw": "RATIONALE\n- path: app.py · why: fix it",
        "parse_status": "parsed",
        "paths": [{"path": "app.py", "why": "fix it"}],
        "deviations": [],
        "parser_version": 1,
    }


def test_poll_turn_skips_interrupted_turn(monkeypatch):
    import sqlmodel

    class Query:
        def where(self, *args):
            return self

        def order_by(self, *args):
            return self

    class Result:
        def first(self):
            return type(
                "Turn",
                (),
                {
                    "seq": 2,
                    "terminal_reason": "interrupted",
                    "usage_json": None,
                },
            )()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec(self, query):
            return Result()

    monkeypatch.setattr(sqlmodel, "Session", lambda engine: Session())
    monkeypatch.setattr(sqlmodel, "select", lambda model: Query())
    monkeypatch.setattr("core.db.get_engine", lambda: object())

    assert steps.poll_turn.__wrapped__(101, 1) is None


def test_usage_counts_parses_well_formed_payload():
    assert steps._usage_counts(
        '{"activities": [{"type": "tool"}, {"type": "tool"}], "input_tokens": "42"}'
    ) == (2, 42)


def test_usage_counts_accepts_none():
    assert steps._usage_counts(None) == (None, None)


def test_usage_counts_rejects_invalid_json():
    assert steps._usage_counts("not json") == (None, None)


def test_usage_counts_rejects_json_scalar():
    assert steps._usage_counts("42") == (None, None)


def test_usage_counts_rejects_non_list_activities():
    """A bad activities shape must not cost the token count as well."""
    assert steps._usage_counts(
        '{"activities": {"type": "tool"}, "input_tokens": 42}'
    ) == (None, 42)


def test_usage_counts_rejects_uncoercible_input_tokens():
    """Nor the reverse: a missing or unusable token count keeps the calls."""
    assert steps._usage_counts('{"activities": [], "input_tokens": "many"}') == (
        0,
        None,
    )
    assert steps._usage_counts('{"activities": [{"type": "tool"}]}') == (1, None)


def test_provider_evidence_reads_the_guest_model_and_effort():
    from factory.orchestration.steps import provider_evidence

    usage = {
        "input_tokens": 10,
        "provider_model": "claude-opus-5-5",
        "effort": "xhigh",
    }
    assert provider_evidence(json.dumps(usage)) == {
        "provider_model": "claude-opus-5-5",
        "effort": "xhigh",
    }
    assert provider_evidence(json.dumps({"input_tokens": 10})) == {}
    assert provider_evidence(json.dumps({"effort": 3, "provider_model": ""})) == {}
    assert provider_evidence(None) == {}
    assert provider_evidence("not json") == {}
