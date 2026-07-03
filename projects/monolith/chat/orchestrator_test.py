"""Tests for chat.orchestrator: the ADR 036 brief compiler.

Covers deterministic prompt assembly, route-partitioned parsing, and the
consent-gated fail-open compile path (including that every path that calls the
model writes exactly one telemetry row, and the ungranted path writes none).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.attention_log_test.
"""

import json
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import acl, orchestrator, orchestrator_client
from chat.models import OrchestratorBrief
from chat.orchestrator import (
    Brief,
    BriefParseError,
    ChatVerdict,
    Directive,
    FailOpen,
    PlanVerdict,
    RequestContext,
    assemble_prompt,
    parse_brief,
)
from chat.orchestrator_client import OrchestratorResponse
from chat.orchestrator_plan import Plan


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _rows(engine) -> list[OrchestratorBrief]:
    with Session(engine) as session:
        return list(session.exec(select(OrchestratorBrief)).all())


_GOOSE_JSON = json.dumps(
    {
        "route": "goose",
        "recipe": "implement",
        "repo": "jomcgi/homelab",
        "repo_paths": ["projects/monolith/chat/bot.py"],
        "hints": "the flow lives in start_agent_flow",
        "constraints": "do not change the failopen path",
        "done_criteria": ["CI green", "PR opened"],
        "stages": [{"title": "read the flow"}, {"title": "wire the verdict"}],
    }
)

_CHAT_JSON = json.dumps(
    {
        "route": "chat",
        "reply_guidance": {
            "context": "the user is asking about boat names",
            "direction": "be playful, offer two options",
            "redirect": "",
        },
    }
)


# ---------------------------------------------------------------------------
# assemble_prompt
# ---------------------------------------------------------------------------


class TestAssemblePrompt:
    def test_is_byte_deterministic(self):
        args = (
            "BUNDLE\n",
            Directive(version=3, text="be concise"),
            ["kg one", "kg two"],
            "channel window",
            "please do the thing",
        )
        first = assemble_prompt(*args)
        second = assemble_prompt(*args)
        assert first == second

    def test_system_carries_bundle_and_versioned_directive_only(self):
        system, user = assemble_prompt(
            "BUNDLE",
            Directive(version=7, text="house style"),
            ["secret kg result"],
            "volatile channel window",
            "the request text",
        )
        assert system.startswith("BUNDLE")
        assert "version 7" in system
        assert "house style" in system
        # Volatile content must never leak into the cached system prefix.
        assert "secret kg result" not in system
        assert "volatile channel window" not in system
        assert "the request text" not in system

    def test_user_carries_volatile_tail(self):
        _system, user = assemble_prompt(
            "BUNDLE",
            Directive(),
            ["kg a"],
            "chan ctx",
            "do it",
        )
        assert "kg a" in user
        assert "chan ctx" in user
        assert "do it" in user

    def test_empty_directive_and_kg_render_placeholders(self):
        system, user = assemble_prompt("BUNDLE", Directive(), [], "", "req")
        assert "(no channel directive set)" in system
        assert "(none)" in user

    def test_similar_messages_render_under_labeled_block(self):
        _system, user = assemble_prompt(
            "BUNDLE",
            Directive(),
            ["[2026-07-03 10:00] alice: is stars broken?"],
            "chan ctx",
            "do it",
        )
        assert "## Contextually similar past messages in this channel" in user
        assert "- [2026-07-03 10:00] alice: is stars broken?" in user
        # The channel context stays a separate, distinct block.
        assert "## Channel context" in user


# ---------------------------------------------------------------------------
# parse_brief
# ---------------------------------------------------------------------------


class TestParseBrief:
    def test_goose_happy(self):
        brief = parse_brief(_GOOSE_JSON, allowed_scopes=frozenset({"jomcgi/homelab"}))
        assert isinstance(brief, Brief)
        assert brief.recipe == "implement"
        assert brief.repo == "jomcgi/homelab"
        assert brief.stages == ["read the flow", "wire the verdict"]
        assert brief.done_criteria == ["CI green", "PR opened"]
        assert brief.repo_replaced is False

    def test_chat_happy(self):
        verdict = parse_brief(_CHAT_JSON, allowed_scopes=frozenset())
        assert isinstance(verdict, ChatVerdict)
        assert verdict.context.startswith("the user is asking")
        assert verdict.direction.startswith("be playful")
        assert verdict.redirect == ""

    def test_tolerates_json_fences(self):
        fenced = "```json\n" + _CHAT_JSON + "\n```"
        verdict = parse_brief(fenced, allowed_scopes=frozenset())
        assert isinstance(verdict, ChatVerdict)

    def test_unknown_keys_tolerated(self):
        data = json.loads(_GOOSE_JSON)
        data["surprise"] = "ignored"
        brief = parse_brief(
            json.dumps(data), allowed_scopes=frozenset({"jomcgi/homelab"})
        )
        assert isinstance(brief, Brief)

    def test_missing_goose_key_raises(self):
        data = json.loads(_GOOSE_JSON)
        del data["stages"]
        with pytest.raises(BriefParseError):
            parse_brief(json.dumps(data), allowed_scopes=frozenset({"jomcgi/homelab"}))

    def test_chat_missing_direction_raises(self):
        data = {"route": "chat", "reply_guidance": {"context": "x"}}
        with pytest.raises(BriefParseError):
            parse_brief(json.dumps(data), allowed_scopes=frozenset())

    def test_unknown_route_raises(self):
        with pytest.raises(BriefParseError):
            parse_brief(json.dumps({"route": "sideways"}), allowed_scopes=frozenset())

    def test_invalid_json_raises(self):
        with pytest.raises(BriefParseError):
            parse_brief("not json at all", allowed_scopes=frozenset())

    def test_repo_out_of_scope_is_discarded_and_flagged(self):
        # Invoker holds a different repo, so the brief's repo is discarded.
        brief = parse_brief(_GOOSE_JSON, allowed_scopes=frozenset({"weave-hand/loom"}))
        assert isinstance(brief, Brief)
        assert brief.repo == ""
        assert brief.repo_replaced is True


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def _ctx(**kw) -> RequestContext:
    base = dict(
        request="do the thing",
        guild_id="g1",
        channel_id="c1",
        thread_id="t1",
        invoker_scope="jomcgi/homelab",
        allowed_scopes=frozenset({"jomcgi/homelab"}),
        directive=Directive(version=2, text="d"),
    )
    base.update(kw)
    return RequestContext(**base)


def _response(content: str) -> OrchestratorResponse:
    return OrchestratorResponse(
        content=content,
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=80,
        latency_ms=42,
    )


# A valid submit_plan tool-call arguments object (passes validate_plan): every
# stepped sub-recipe is a catalog id and appears in enabled_subrecipes.
_PLAN_ARGS = {
    "enabled_subrecipes": ["query", "implement"],
    "steps": [
        {"sub_recipe": "query", "context": "read start_agent_flow in bot.py"},
        {"sub_recipe": "implement", "context": "wire the verdict through, open a PR"},
    ],
    "done_criteria": ["CI green", "PR opened"],
}


def _tool_response() -> OrchestratorResponse:
    """The plan (second) call's response. Distinct latency so the goose row's
    ``plan_latency_ms`` is provably sourced from the plan call, not the route
    call (which is 42ms above)."""
    return OrchestratorResponse(
        content=json.dumps(_PLAN_ARGS),
        prompt_tokens=200,
        completion_tokens=30,
        cached_tokens=150,
        latency_ms=33,
    )


def _plan_call(args: dict | None = None) -> AsyncMock:
    """Mock ``orchestrator_client.call_tool`` returning ``(args, response)``."""
    return AsyncMock(
        return_value=(args if args is not None else _PLAN_ARGS, _tool_response())
    )


class TestCompile:
    @pytest.mark.asyncio
    async def test_ungranted_no_call_no_row(self, engine, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: False)
        spy = AsyncMock(side_effect=AssertionError("client must not be called"))
        monkeypatch.setattr(orchestrator_client, "call", spy)
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        spy.assert_not_called()
        assert _rows(engine) == []

    @pytest.mark.asyncio
    async def test_disabled_no_call_no_row(self, engine, monkeypatch):
        monkeypatch.delenv("ORCHESTRATOR_MODEL", raising=False)
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        spy = AsyncMock(side_effect=AssertionError("client must not be called"))
        monkeypatch.setattr(orchestrator_client, "call", spy)
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        spy.assert_not_called()
        assert _rows(engine) == []

    @pytest.mark.asyncio
    async def test_happy_goose_returns_plan_verdict_writes_one_row(
        self, engine, monkeypatch
    ):
        # Goose route: the route call decides goose, the second submit_plan call
        # yields the plan. compile returns a PlanVerdict and writes EXACTLY ONE
        # telemetry row (the plan call does not write its own row).
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client, "call", AsyncMock(return_value=_response(_GOOSE_JSON))
        )
        monkeypatch.setattr(orchestrator_client, "call_tool", _plan_call())
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, PlanVerdict)
        # The plan is the parsed, validated typed Plan.
        assert isinstance(verdict.plan, Plan)
        assert [s.sub_recipe for s in verdict.plan.steps] == ["query", "implement"]
        # Repo scope carried from the route-decision Brief (invoker holds it).
        assert verdict.repo == "jomcgi/homelab"
        assert verdict.repo_paths == ["projects/monolith/chat/bot.py"]
        assert verdict.repo_replaced is False

        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "goose"
        assert rows[0].model == "test/model"
        assert rows[0].thread_id == "t1"
        # Route-call token/latency accounting stays in the dedicated columns.
        assert rows[0].cached_tokens == 80
        assert rows[0].latency_ms == 42
        assert rows[0].error is None
        # Plan fields ride in the existing brief_json column (no new DB column).
        assert rows[0].brief_json["plan_step_count"] == 2
        assert rows[0].brief_json["plan_latency_ms"] == 33
        assert rows[0].brief_json["plan"]["steps"][0]["sub_recipe"] == "query"
        assert rows[0].brief_json["repo"] == "jomcgi/homelab"

    @pytest.mark.asyncio
    async def test_plan_call_unavailable_fails_open_one_row(self, engine, monkeypatch):
        # Route call succeeds (goose), but the second submit_plan call is
        # unavailable: compile fails open to today's baked recipe path, writing
        # exactly one failopen row.
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client, "call", AsyncMock(return_value=_response(_GOOSE_JSON))
        )
        monkeypatch.setattr(
            orchestrator_client,
            "call_tool",
            AsyncMock(
                side_effect=orchestrator_client.OrchestratorUnavailable(
                    "plan timed out"
                )
            ),
        )
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "failopen"
        assert rows[0].brief_json is None
        assert "plan timed out" in rows[0].error

    @pytest.mark.asyncio
    async def test_invalid_plan_fails_open_one_row(self, engine, monkeypatch):
        # Route call succeeds (goose), but the plan is semantically invalid
        # (empty steps): validate_plan errors force a fail-open, one failopen row.
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client, "call", AsyncMock(return_value=_response(_GOOSE_JSON))
        )
        empty = {"enabled_subrecipes": [], "steps": [], "done_criteria": []}
        monkeypatch.setattr(orchestrator_client, "call_tool", _plan_call(empty))
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "failopen"
        assert rows[0].brief_json is None
        assert "invalid plan" in rows[0].error

    @pytest.mark.asyncio
    async def test_happy_chat_writes_chat_row(self, engine, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client, "call", AsyncMock(return_value=_response(_CHAT_JSON))
        )
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, ChatVerdict)
        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "chat"
        assert rows[0].brief_json["direction"].startswith("be playful")

    @pytest.mark.asyncio
    async def test_timeout_writes_failopen_row(self, engine, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client,
            "call",
            AsyncMock(
                side_effect=orchestrator_client.OrchestratorUnavailable("timed out")
            ),
        )
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "failopen"
        assert rows[0].brief_json is None
        assert "timed out" in rows[0].error

    @pytest.mark.asyncio
    async def test_parse_failure_writes_failopen_row(self, engine, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client,
            "call",
            AsyncMock(return_value=_response("this is not json")),
        )
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        verdict = await orchestrator.compile(_ctx())
        assert isinstance(verdict, FailOpen)
        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0].route == "failopen"
        # Token counts from the (parsed-but-unusable) response are still recorded.
        assert rows[0].prompt_tokens == 100

    @pytest.mark.asyncio
    async def test_repo_out_of_scope_uses_invoker_scope(self, engine, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "test/model")
        monkeypatch.setattr(acl, "is_granted", lambda *a, **k: True)
        monkeypatch.setattr(
            orchestrator_client, "call", AsyncMock(return_value=_response(_GOOSE_JSON))
        )
        monkeypatch.setattr(orchestrator_client, "call_tool", _plan_call())
        monkeypatch.setattr(orchestrator, "get_engine", lambda: engine)

        # Invoker holds only loom, but the brief named jomcgi/homelab.
        ctx = _ctx(
            invoker_scope="weave-hand/loom",
            allowed_scopes=frozenset({"weave-hand/loom"}),
        )
        verdict = await orchestrator.compile(ctx)
        assert isinstance(verdict, PlanVerdict)
        # The out-of-scope repo replacement carries through to the PlanVerdict.
        assert verdict.repo == "weave-hand/loom"
        assert verdict.repo_replaced is True
        assert _rows(engine)[0].brief_json["repo_replaced"] is True


class TestReplan:
    """The capped replan escape hatch's orchestrator side (Task 7).

    ``orchestrator.replan`` re-invokes ``submit_plan`` with the original request,
    the prior plan, and goose's replan feedback, then returns a validated Plan or
    None (fail-open). It writes no telemetry row (a separate call outside
    ``compile``), so these tests do not touch the exactly-one-row accounting.
    """

    def _prior_plan(self) -> Plan:
        from chat.orchestrator_plan import PlanStep

        return Plan(
            enabled_subrecipes=("query",),
            steps=(PlanStep(sub_recipe="query", context="answer it"),),
            done_criteria=("answered",),
        )

    @pytest.mark.asyncio
    async def test_replan_returns_validated_plan(self, monkeypatch):
        monkeypatch.setattr(orchestrator_client, "call_tool", _plan_call())
        plan = await orchestrator.replan(
            "the original request",
            self._prior_plan(),
            reason="query cannot open a PR",
            what_i_learned="needs a code change",
            suggested_focus="use implement",
        )
        assert isinstance(plan, Plan)
        assert [s.sub_recipe for s in plan.steps] == ["query", "implement"]

    @pytest.mark.asyncio
    async def test_replan_passes_replan_timeout(self, monkeypatch):
        captured = {}

        async def fake_call_tool(system, user, *, schema, timeout_s=None, **kw):
            captured["timeout_s"] = timeout_s
            captured["user"] = user
            return _PLAN_ARGS, _tool_response()

        monkeypatch.setattr(orchestrator_client, "call_tool", fake_call_tool)
        await orchestrator.replan(
            "orig",
            self._prior_plan(),
            reason="r",
            what_i_learned="l",
            suggested_focus="f",
        )
        assert captured["timeout_s"] == orchestrator._replan_timeout_s()
        # The user prompt carries the original request and the replan feedback.
        assert "orig" in captured["user"]
        assert "l" in captured["user"]

    @pytest.mark.asyncio
    async def test_replan_unavailable_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator_client,
            "call_tool",
            AsyncMock(
                side_effect=orchestrator_client.OrchestratorUnavailable("timeout")
            ),
        )
        plan = await orchestrator.replan(
            "orig",
            self._prior_plan(),
            reason="r",
            what_i_learned="l",
            suggested_focus="f",
        )
        assert plan is None

    @pytest.mark.asyncio
    async def test_replan_invalid_plan_returns_none(self, monkeypatch):
        # An empty step list fails validate_plan -> fail-open (None).
        empty = {"enabled_subrecipes": [], "steps": [], "done_criteria": []}
        monkeypatch.setattr(orchestrator_client, "call_tool", _plan_call(empty))
        plan = await orchestrator.replan(
            "orig",
            self._prior_plan(),
            reason="r",
            what_i_learned="l",
            suggested_focus="f",
        )
        assert plan is None


class TestReplanTimeoutConfig:
    """The env-driven replan-timeout resolution (Task 8)."""

    def test_default_is_120_when_unset(self, monkeypatch):
        monkeypatch.delenv("ORCHESTRATOR_REPLAN_TIMEOUT_S", raising=False)
        assert orchestrator._replan_timeout_s() == 120.0

    def test_respects_env_value(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_REPLAN_TIMEOUT_S", "200")
        assert orchestrator._replan_timeout_s() == 200.0

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_REPLAN_TIMEOUT_S", "not-a-number")
        assert orchestrator._replan_timeout_s() == 120.0
