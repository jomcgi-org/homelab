import httpx
import pytest

import swarm.steps as steps


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


def test_pin_plan_resolves_config_once(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("SWARM_IMPLEMENTER_MODEL", "implementer")
    monkeypatch.setenv("SWARM_REVIEWER_MODEL", "reviewer")
    monkeypatch.setenv("SWARM_TURN_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("SWARM_DECISION_TIMEOUT_SECONDS", "84")

    assert steps.pin_plan.__wrapped__(2.0) == {
        "version": 1,
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


def test_start_agent_session_forwards_workflow_id(monkeypatch):
    import agent_sessions.api as api

    calls = []

    def fake_start_session(*args, **kwargs):
        calls.append((args, kwargs))
        return 101

    monkeypatch.setattr(api, "start_session_for_swarm", fake_start_session)

    result = steps.start_agent_session.__wrapped__(
        "test-key", "prompt", "luna", "jomcgi/homelab", "main", workflow_id="wf-abc"
    )

    assert result == 101
    assert calls == [
        (
            ("test-key", "prompt", "luna", "jomcgi/homelab", "main"),
            {
                "workflow_id": "wf-abc",
                "node_key": None,
                "node_attempt": None,
            },
        )
    ]


def test_poll_turn_includes_rationale(monkeypatch):
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
                    "terminal_reason": "completed",
                    "cost_usd": 0.25,
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

    assert payload["rationale"] == {
        "raw": "RATIONALE\n- path: app.py · why: fix it",
        "parse_status": "parsed",
        "paths": [{"path": "app.py", "why": "fix it"}],
        "deviations": [],
        "parser_version": 1,
    }
