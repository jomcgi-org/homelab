"""Tests for the capped DeepSeek replan escape hatch (Task 7).

Two surfaces:

- ``goosecracker.replan.parse_replan``: extract the structured ``replan`` signal
  out of goose's ``recipe__final_output`` JSON (returned verbatim in
  ``AgentResult.Result``), tolerating a bare object, a fenced block, or a JSON
  object on the trailing line of a transcript; None for absent / all-empty /
  malformed / non-JSON.
- The ``_run_one_turn`` replan loop: on a plan-driven turn whose result carries a
  replan request it re-invokes the orchestrator and re-runs the turn, capped at
  ``_MAX_REPLANS``; interim replan-request results are never delivered, only the
  final one. The non-plan path never inspects a result for a replan.

The loop tests mock at the run/deliver seams (``_invoke_turn`` /
``_deliver_result``) and stub ``chat.api`` in ``sys.modules`` (the loop lazily
does ``from chat.api import replan``, so a sys.modules entry wins).
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from goosecracker import replan, runner

# ---------------------------------------------------------------------------
# parse_replan
# ---------------------------------------------------------------------------


def _final_output(**fields) -> str:
    return json.dumps({"summary": "s", **fields})


def test_parse_replan_returns_request_when_populated():
    text = _final_output(
        replan={
            "reason": "query sub-recipe cannot open a PR",
            "what_i_learned": "the task needs a code change, not an answer",
            "suggested_focus": "use implement",
        }
    )
    req = replan.parse_replan(text)
    assert req is not None
    assert req.reason == "query sub-recipe cannot open a PR"
    assert req.what_i_learned == "the task needs a code change, not an answer"
    assert req.suggested_focus == "use implement"


def test_parse_replan_partial_fields_still_returns_request():
    # Only one field populated is still a genuine request.
    req = replan.parse_replan(_final_output(replan={"suggested_focus": "use research"}))
    assert req is not None
    assert req.suggested_focus == "use research"
    assert req.reason == ""


def test_parse_replan_none_when_replan_absent():
    assert replan.parse_replan(_final_output()) is None


def test_parse_replan_none_when_replan_all_empty():
    text = _final_output(
        replan={"reason": "", "what_i_learned": "  ", "suggested_focus": ""}
    )
    assert replan.parse_replan(text) is None


def test_parse_replan_none_when_replan_not_object():
    assert replan.parse_replan(_final_output(replan="please replan")) is None


def test_parse_replan_none_on_malformed_json():
    assert replan.parse_replan("{ not valid json }") is None


def test_parse_replan_none_on_non_json():
    assert replan.parse_replan("just some prose, no json here") is None


def test_parse_replan_none_on_empty():
    assert replan.parse_replan("") is None


def test_parse_replan_reads_trailing_json_line_of_transcript():
    # goose's response.json_schema output is the last line of a longer transcript.
    transcript = (
        "Loading recipe: Runtime Router\n"
        "  __( O)>  goose is ready\n"
        + _final_output(replan={"reason": "blocked", "what_i_learned": "x"})
    )
    req = replan.parse_replan(transcript)
    assert req is not None
    assert req.reason == "blocked"


def test_parse_replan_reads_fenced_json():
    fenced = (
        "```json\n"
        + _final_output(replan={"suggested_focus": "narrow to one file"})
        + "\n```"
    )
    req = replan.parse_replan(fenced)
    assert req is not None
    assert req.suggested_focus == "narrow to one file"


# ---------------------------------------------------------------------------
# _run_one_turn replan loop
# ---------------------------------------------------------------------------


def _plan(sub_recipe: str, context: str = "ctx"):
    from chat.orchestrator_plan import Plan, PlanStep

    return Plan(
        enabled_subrecipes=(sub_recipe,),
        steps=(PlanStep(sub_recipe=sub_recipe, context=context),),
        done_criteria=("done",),
    )


_REPLAN_RESULT = json.dumps(
    {"summary": "requesting a replan", "replan": {"reason": "misfit"}}
)
_CLEAN_RESULT = json.dumps({"summary": "all done"})


def _wire_loop(monkeypatch, *, invoke_results, replan_returns):
    """Stub the run/deliver seams and the orchestrator; return the recorders.

    ``invoke_results`` scripts what each ``_invoke_turn`` returns (as fc-invoke
    ``data`` dicts). ``replan_returns`` is the ``orchestrator.replan`` side-effect
    (a list of Plans / None). Returns (invoke_plans, delivered, fake_orch).
    """
    invoke_plans: list = []

    async def fake_invoke(session, **kwargs):
        invoke_plans.append(kwargs["plan"])
        return invoke_results[len(invoke_plans) - 1]

    delivered: list = []

    async def fake_deliver(session, discord_thread, data, provider="discord"):
        delivered.append(data)
        return True

    monkeypatch.setattr(runner, "_invoke_turn", fake_invoke)
    monkeypatch.setattr(runner, "_deliver_result", fake_deliver)
    monkeypatch.setattr(runner, "_mark_progress_done", MagicMock())

    fake_orch = MagicMock()
    fake_orch.replan = AsyncMock(side_effect=replan_returns)
    return invoke_plans, delivered, fake_orch


async def _run(fake_orch, *, plan):
    # runner._request_replan does `from chat.api import replan` (import-boundary
    # rule: domains import from chat.api, not chat internals), so stub chat.api
    # in sys.modules -- not chat.orchestrator. Stubbing the module also means the
    # import resolves to the mock without hitting the real chat.api import graph
    # (whose beartype claw hook otherwise trips a circular import here).
    with patch.dict(sys.modules, {"chat.api": fake_orch}):
        return await runner._run_one_turn(
            "sess",
            task="do the thing",
            recipe="agent",
            tier="",
            git_mirror="",
            git_ref="",
            discord_thread="T",
            plan=plan,
        )


async def test_replans_twice_then_delivers_clean_result_once(monkeypatch):
    """Two replan-request results, then a clean one: orchestrator.replan is called
    twice, the turn re-runs with each revised plan, and only the final clean
    result is delivered (interim results never settled)."""
    p0, p1, p2 = _plan("query"), _plan("research"), _plan("implement")
    invoke_plans, delivered, fake_orch = _wire_loop(
        monkeypatch,
        invoke_results=[
            {"status": "ok", "result": _REPLAN_RESULT},
            {"status": "ok", "result": _REPLAN_RESULT},
            {"status": "ok", "result": _CLEAN_RESULT},
        ],
        replan_returns=[p1, p2],
    )

    ok = await _run(fake_orch, plan=p0)

    assert ok is True
    assert fake_orch.replan.await_count == 2
    # The turn re-ran with each revised plan, in order.
    assert invoke_plans == [p0, p1, p2]
    # Only the final clean result was delivered; the two interim ones were not.
    assert len(delivered) == 1
    assert delivered[0]["result"] == _CLEAN_RESULT


async def test_replan_capped_at_max(monkeypatch):
    """A result that keeps requesting a replan is capped at _MAX_REPLANS
    orchestrator calls, then the current (still replan-request) result is
    delivered exactly once (budget-exhausted finalize)."""
    revised = _plan("implement")
    invoke_plans, delivered, fake_orch = _wire_loop(
        monkeypatch,
        # Always returns a replan request.
        invoke_results=[{"status": "ok", "result": _REPLAN_RESULT}] * 10,
        replan_returns=[revised] * 10,
    )

    ok = await _run(fake_orch, plan=_plan("query"))

    assert ok is True
    assert fake_orch.replan.await_count == runner._MAX_REPLANS  # exactly 3
    # _MAX_REPLANS replans -> _MAX_REPLANS + 1 goose invocations.
    assert len(invoke_plans) == runner._MAX_REPLANS + 1
    assert len(delivered) == 1  # finalized once at the cap


async def test_replan_none_finalizes_immediately(monkeypatch):
    """orchestrator.replan returning None (unavailable / invalid) stops the loop
    at once and finalizes with the current result (fail-open)."""
    invoke_plans, delivered, fake_orch = _wire_loop(
        monkeypatch,
        invoke_results=[{"status": "ok", "result": _REPLAN_RESULT}],
        replan_returns=[None],
    )

    ok = await _run(fake_orch, plan=_plan("query"))

    assert ok is True
    assert fake_orch.replan.await_count == 1
    assert len(invoke_plans) == 1  # no re-run
    assert len(delivered) == 1  # the current result is delivered (fail-open)


async def test_non_plan_turn_never_replans(monkeypatch):
    """With plan=None the loop never inspects the result for a replan, even if the
    result happens to carry a replan-shaped object, and delivers exactly as
    today."""
    invoke_plans, delivered, fake_orch = _wire_loop(
        monkeypatch,
        invoke_results=[{"status": "ok", "result": _REPLAN_RESULT}],
        replan_returns=[_plan("implement")],
    )

    ok = await _run(fake_orch, plan=None)

    assert ok is True
    fake_orch.replan.assert_not_called()
    assert invoke_plans == [None]
    assert len(delivered) == 1
    assert delivered[0]["result"] == _REPLAN_RESULT
