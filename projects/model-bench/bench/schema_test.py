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


def test_modelspec_defaults_active_and_temp_zero():
    m = ModelSpec(id="anthropic/claude-sonnet-4.6")
    assert m.status == "active"
    assert m.params.temperature == 0.0


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
