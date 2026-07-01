from bench.judge import _parse_verdict, judge_free_text, JudgeConfig


def fake_caller(prompt: str) -> str:
    # deterministic stub: returns PASS iff the candidate contains "feat("
    return "PASS" if "feat(" in prompt else "FAIL"


def test_parse_verdict_reads_reasoned_reply():
    # A reasoned reply starts with per-criterion lines and ends with VERDICT.
    reasoned_pass = (
        "1. Met - subject ok\n2. Met - body ok\n3. Met - no em-dash\n\nVERDICT: PASS"
    )
    reasoned_fail = "1. Met\n2. Not met - body missing\n\nVERDICT: FAIL"
    assert _parse_verdict(reasoned_pass) == "PASS"
    assert _parse_verdict(reasoned_fail) == "FAIL"
    # Bare verdicts still parse.
    assert _parse_verdict("PASS") == "PASS"
    assert _parse_verdict("FAIL") == "FAIL"
    # Reasoning that mentions FAIL earlier but ends VERDICT: PASS reads the verdict line.
    assert (
        _parse_verdict("could FAIL if empty, but it is fine.\nVERDICT: PASS") == "PASS"
    )


def test_judge_passes_conventional_commit():
    cfg = JudgeConfig(
        judge_model="anthropic/claude-sonnet-4.6",
        criteria=["conventional", "no-em-dash"],
    )
    r = judge_free_text(
        candidate="feat(x): do a thing",
        task_prompt="write a commit",
        cfg=cfg,
        caller=fake_caller,
    )
    assert r.passed


def test_judge_refuses_to_grade_own_output():
    import pytest

    cfg = JudgeConfig(judge_model="anthropic/claude-sonnet-4.6", criteria=["x"])
    with pytest.raises(ValueError):
        judge_free_text(
            candidate="c",
            task_prompt="p",
            cfg=cfg,
            caller=fake_caller,
            candidate_model="anthropic/claude-sonnet-4.6",
        )
