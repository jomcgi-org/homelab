import pytest  # noqa: F401

from bench.schema import (
    TaskSpec,
    VerifierSpec,
    ModelSpec,
    ResultCell,
    TaskClass,
    Attempt,
)


def test_taskspec_parses_minimal_yaml_shape():
    t = TaskSpec(
        id="helm-values-plumbing-01",
        version="v1",
        task_class=TaskClass.CONFIG_PLUMBING,
        prompt="Wire the image tag into values.yaml.",
        target_files=["values.yaml"],
        verifier=VerifierSpec(
            kind="helm-template", args={"release": "x", "assert_jsonpath": "$.spec"}
        ),
    )
    assert t.task_class == "config-plumbing"
    assert t.verifier.kind == "helm-template"


def test_taskspec_mode_defaults_single_shot_with_agent_budget():
    # A task with no mode is single-shot; the agent budget carries safe defaults so
    # an agentic task can omit them, and target_files is optional (agentic tasks edit
    # via tools rather than declaring a target).
    t = TaskSpec(
        id="t",
        version="v1",
        task_class=TaskClass.CODE_FIX,
        prompt="fix it",
        verifier=VerifierSpec(kind="pytest"),
    )
    assert t.mode == "single-shot"
    assert t.target_files == []
    assert t.agent.max_turns == 20 and t.agent.max_tokens is None


def test_taskspec_parses_agentic_mode_and_budget():
    t = TaskSpec(
        id="t",
        version="v1",
        task_class=TaskClass.CODE_FIX,
        mode="agentic",
        prompt="fix it",
        verifier=VerifierSpec(kind="pytest"),
        agent={"max_turns": 30, "max_tokens": 4096},
    )
    assert t.mode == "agentic"
    assert t.agent.max_turns == 30 and t.agent.max_tokens == 4096


def test_resultcell_agentic_signals_default_none():
    cell = ResultCell(
        task_id="t",
        task_version="v1",
        model_id="m",
        content_hash="h",
        outcome="fail",
        attempts=[
            Attempt(
                passed=False,
                feedback="",
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
            )
        ],
        cost_usd=0.0,
        harness_version="0.1.0",
        prompt_template_hash="x",
    )
    assert cell.turns is None and cell.tool_use_ok is None


def test_modelspec_defaults_active_and_temp_zero():
    m = ModelSpec(id="anthropic/claude-sonnet-4.6")
    assert m.status == "active"
    assert m.params.temperature == 0.0
    assert m.api_model is None
    assert m.extra_body == {}


def test_modelspec_api_model_and_extra_body_round_trip():
    m = ModelSpec(
        id="qwen/qwen3.8-27b",
        api_model="qwen3.6-27b",
        extra_body={"chat_template_kwargs": {"reasoning_effort": "xhigh"}},
    )
    assert m.api_model == "qwen3.6-27b"
    assert m.extra_body == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}
    again = ModelSpec.model_validate(m.model_dump())
    assert again.api_model == m.api_model
    assert again.extra_body == m.extra_body


def test_resultcell_records_both_attempts_and_provenance():
    cell = ResultCell(
        task_id="t",
        task_version="v1",
        model_id="m",
        content_hash="abc123",
        outcome="pass@2",
        attempts=[
            Attempt(
                passed=False,
                feedback="err",
                latency_ms=10,
                prompt_tokens=5,
                completion_tokens=7,
            ),
            Attempt(
                passed=True,
                feedback="",
                latency_ms=20,
                prompt_tokens=9,
                completion_tokens=3,
            ),
        ],
        cost_usd=0.0004,
        harness_version="0.1.0",
        prompt_template_hash="deadbeef",
    )
    assert cell.total_latency_ms == 30
    assert cell.total_tokens == 24
    assert cell.first_attempt_passed is False
